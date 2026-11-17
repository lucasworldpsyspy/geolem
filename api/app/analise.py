"""Análise espacial de conformidade: cruza uma área com as bases carregadas no PostGIS."""
import json
import secrets
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from . import regras
from .config import AREA_MAXIMA_HA, AREA_MINIMA_HA
from .laudo_pdf import codigo_integridade


class ErroAnalise(ValueError):
    pass


SQL_AREA = "WITH a AS (SELECT geo.normalizar(ST_GeomFromGeoJSON(%(g)s)) AS g)"


def _sobreposicoes(cur, tabela: str, colunas: str, geojson: str) -> list[dict]:
    # `tabela` e `colunas` vêm só de constantes deste módulo, nunca do usuário.
    cur.execute(
        f"""{SQL_AREA}
        SELECT {colunas},
               ST_AsGeoJSON(i.gi, 6) AS geom_json,
               ST_Area(i.gi::geography) / 10000.0 AS ha
        FROM a
        JOIN {tabela} t ON ST_Intersects(t.geom, a.g)
        CROSS JOIN LATERAL (SELECT ST_CollectionExtract(ST_Intersection(t.geom, a.g), 3) AS gi) i
        WHERE NOT ST_IsEmpty(i.gi)""",
        {"g": geojson},
    )
    return [r for r in cur.fetchall() if r["ha"] >= AREA_MINIMA_HA]


def _normalizar_nome(nome: str | None) -> str:
    sem_acento = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return " ".join(sem_acento.upper().split())


def _bases(cur) -> dict[str, list[dict]]:
    cur.execute(
        """SELECT DISTINCT ON (fonte) fonte, tabela, descricao, url, concluido_em, registros
           FROM geo.cargas WHERE status = 'ok' ORDER BY fonte, concluido_em DESC"""
    )
    por_tabela: dict[str, list[dict]] = defaultdict(list)
    for r in cur.fetchall():
        por_tabela[r["tabela"]].append(
            {
                "fonte": r["fonte"],
                "descricao": r["descricao"],
                "url": r["url"],
                "atualizado_em": r["concluido_em"].isoformat() if r["concluido_em"] else None,
                "registros": r["registros"],
            }
        )
    return por_tabela


def _data_br(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "data não informada"


def analisar(conn, geometria: dict, nome: str | None = None, imovel: dict | None = None,
             reserva_legal_ha: float | None = None) -> dict:
    g = json.dumps(geometria)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""{SQL_AREA}
                SELECT ST_IsEmpty(a.g) AS vazia,
                       ST_Area(a.g::geography) / 10000.0 AS ha,
                       ST_X(ST_PointOnSurface(a.g)) AS lon,
                       ST_Y(ST_PointOnSurface(a.g)) AS lat,
                       ST_AsGeoJSON(a.g, 6) AS geom_json
                FROM a""",
                {"g": g},
            )
        except Exception as e:  # GeoJSON malformado
            raise ErroAnalise(f"Geometria inválida: {e}") from e
        area = cur.fetchone()
        if area["vazia"] or not area["ha"]:
            raise ErroAnalise("A geometria enviada não forma uma área (polígono) válida.")
        if area["ha"] > AREA_MAXIMA_HA:
            raise ErroAnalise(f"Área grande demais ({regras.fmt_ha(area['ha'])}). O limite é {regras.fmt_ha(AREA_MAXIMA_HA)}.")

        bases = _bases(cur)
        checks: list[dict] = []
        conflitos: list[dict] = []

        def adicionar(id_: str, titulo: str, base: str, tabela: str, itens: list[dict], status_se_houver: str):
            """itens: [{descricao, ha, geom_json?}]"""
            if tabela not in bases:
                checks.append({"id": id_, "titulo": titulo, "base": base, "status": "warn", "ha": 0,
                               "itens": [{"descricao": "Base ainda não carregada no sistema. Verificação não realizada.", "ha": None}]})
                return
            for i in itens:
                for gj in i.pop("geoms", []):
                    conflitos.append({"type": "Feature", "geometry": json.loads(gj),
                                      "properties": {"status": status_se_houver, "rotulo": titulo}})
            checks.append({
                "id": id_, "titulo": titulo, "base": base,
                "status": status_se_houver if itens else "ok",
                "ha": round(sum(i["ha"] or 0 for i in itens), 2),
                "itens": itens or [{"descricao": "Nenhuma sobreposição encontrada.", "ha": None}],
            })

        # 1-3. Desmatamento (PRODES), agrupado por ano
        grupos: dict[str, dict[int | None, dict]] = {"pos_2020": {}, "pos_2008": {}, "consolidado": {}}
        for r in _sobreposicoes(cur, "geo.desmatamento", "t.ano, t.data_imagem, t.fonte", g):
            classe = regras.classificar_desmatamento(r["ano"], r["data_imagem"])
            gr = grupos[classe].setdefault(r["ano"], {"n": 0, "ha": 0.0, "geoms": [], "datas": []})
            gr["n"] += 1
            gr["ha"] += r["ha"]
            gr["geoms"].append(r["geom_json"])
            if r["data_imagem"]:
                gr["datas"].append(r["data_imagem"])

        def itens_desm(classe):
            itens = []
            for ano, gr in sorted(grupos[classe].items(), key=lambda kv: kv[0] or 0):
                desc = f"PRODES {ano or 'ano não informado'}: {gr['n']} polígono(s)"
                if gr["datas"]:
                    desc += f", imagem de {_data_br(max(gr['datas']))}"
                itens.append({"descricao": desc, "ha": round(gr["ha"], 2), "geoms": gr["geoms"]})
            return itens

        adicionar("desm_pos_2020", "Desmatamento após 31/12/2020",
                  "Data de corte do Regulamento Europeu antidesmatamento (EUDR)", "geo.desmatamento",
                  itens_desm("pos_2020"), "bad")
        adicionar("desm_pos_2008", "Desmatamento após 22/07/2008",
                  "Marco temporal do Código Florestal (Lei 12.651/2012)", "geo.desmatamento",
                  itens_desm("pos_2008"), "warn")
        adicionar("desm_consolidado", "Supressão até 22/07/2008",
                  "Área rural consolidada (informativo)", "geo.desmatamento",
                  itens_desm("consolidado"), "info")

        # 4. Embargos IBAMA
        emb = _sobreposicoes(cur, "geo.embargos", "t.numero_tad, t.serie_tad, t.data_embargo, t.infracao", g)
        adicionar("embargos", "Embargos ambientais", "Áreas embargadas pelo IBAMA", "geo.embargos", [
            {"descricao": f"TAD {r['numero_tad'] or '?'}{('-' + r['serie_tad']) if r['serie_tad'] else ''} de {_data_br(r['data_embargo'])}"
                          + (f": {r['infracao'][:140]}" if r["infracao"] else ""),
             "ha": round(r["ha"], 2), "geoms": [r["geom_json"]]}
            for r in emb
        ], "bad")

        # 5-6. Unidades de Conservação (bases diferentes podem repetir a mesma UC)
        ucs: dict[tuple, dict] = {}
        for r in _sobreposicoes(cur, "geo.unidades_conservacao", "t.nome, t.categoria, t.grupo, t.esfera", g):
            chave = (_normalizar_nome(r["nome"]), r["grupo"])
            atual = ucs.get(chave)
            if atual is None or r["ha"] > atual["ha"]:
                ucs[chave] = r
        for grupo, id_, titulo, base, st in (
            ("PI", "uc_pi", "UC de Proteção Integral", "Lei 9.985/2000 (SNUC): uso direto proibido", "bad"),
            ("US", "uc_us", "UC de Uso Sustentável", "Uso permitido conforme plano de manejo", "warn"),
        ):
            adicionar(id_, titulo, base, "geo.unidades_conservacao", [
                {"descricao": f"{r['nome']} ({r['esfera'] or 'esfera não informada'})", "ha": round(r["ha"], 2),
                 "geoms": [r["geom_json"]]}
                for r in ucs.values() if r["grupo"] == grupo
            ], st)

        # 7. Terras Indígenas
        tis = _sobreposicoes(cur, "geo.terras_indigenas", "t.nome, t.etnia, t.fase", g)
        adicionar("tis", "Terras Indígenas", "Sobreposição com TI (FUNAI)", "geo.terras_indigenas", [
            {"descricao": f"{r['nome']} ({r['fase'] or 'fase não informada'})", "ha": round(r["ha"], 2),
             "geoms": [r["geom_json"]]}
            for r in tis
        ], "bad")

        # 8. Reserva Legal
        rl = reserva_legal_ha if reserva_legal_ha is not None else (imovel or {}).get("reserva_legal_ha")
        st_rl, texto_rl = regras.avaliar_reserva_legal(rl, area["ha"])
        checks.append({"id": "reserva_legal", "titulo": "Reserva Legal (mínimo 20%)",
                       "base": "Art. 12, II, da Lei 12.651/2012 (Cerrado fora da Amazônia Legal)",
                       "status": st_rl, "ha": rl or 0, "itens": [{"descricao": texto_rl, "ha": None}]})

        # 9. Imóveis sobrepostos (identificação, não pesa no resultado)
        imv = _sobreposicoes(cur, "geo.imoveis", "t.id, t.tipo, t.codigo, t.nome", g)
        if "geo.imoveis" in bases:
            checks.append({
                "id": "imoveis", "titulo": "Imóveis cadastrados sobrepostos", "base": "SIGEF (INCRA) e CAR (SICAR)",
                "status": "info" if imv else "ok", "ha": round(sum(r["ha"] for r in imv), 2),
                "itens": [{"descricao": f"{r['tipo']} {r['codigo'] or ''} {('· ' + r['nome']) if r['nome'] else ''}".strip(),
                           "ha": round(r["ha"], 2)} for r in sorted(imv, key=lambda r: -r["ha"])[:15]]
                         or [{"descricao": "Nenhum imóvel cadastrado sobreposto.", "ha": None}],
            })

        geral = regras.status_geral([c["status"] for c in checks if c["id"] != "imoveis"])
        agora = datetime.now(timezone.utc)
        protocolo = f"GL-{agora:%Y%m%d}-{secrets.token_hex(4).upper()}"  # 8 hex: inviável adivinhar
        resultado = {
            "protocolo": protocolo,
            "criado_em": agora.isoformat(),
            "area": {
                "nome": nome or (imovel or {}).get("nome") or "Área sem nome",
                "area_ha": round(area["ha"], 2),
                "ponto_interno": [round(area["lon"], 6), round(area["lat"], 6)],
                "imovel": imovel,
                "geometria": json.loads(area["geom_json"]),
            },
            "geral": geral,
            "rotulo_geral": regras.ROTULOS[geral],
            "resumo": regras.RESUMOS[geral],
            "checks": checks,
            "conflitos": {"type": "FeatureCollection", "features": conflitos},
            "bases": [b for lst in bases.values() for b in lst],
            "parametros": {"area_minima_ha": AREA_MINIMA_HA},
        }
        # Gravado junto e impresso no PDF: qualquer alteração posterior no conteúdo muda o código
        resultado["integridade"] = codigo_integridade(resultado)

        cur.execute(
            """INSERT INTO geo.analises (protocolo, nome_area, imovel_id, area_ha, resultado, detalhes, geom)
               VALUES (%s, %s, %s, %s, %s, %s, geo.normalizar(ST_GeomFromGeoJSON(%s)))""",
            (protocolo, resultado["area"]["nome"], (imovel or {}).get("id"), area["ha"], geral,
             Jsonb(resultado), area["geom_json"]),
        )
    conn.commit()
    return resultado
