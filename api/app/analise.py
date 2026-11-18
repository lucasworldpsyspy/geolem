"""Análise espacial de conformidade: cruza uma área com as bases carregadas no PostGIS."""
import json
import secrets
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from . import imagens, regras
from .config import AREA_MAXIMA_HA, AREA_MINIMA_HA, IMAGENS_SATELITE
from .laudo_pdf import FUSO_BAHIA, codigo_integridade


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
             reserva_legal_ha: float | None = None, empresa_id: int | None = None, usuario_id: int | None = None,
             area_id: int | None = None, gravar: bool = True, com_imagens: bool = True) -> dict:
    g = json.dumps(geometria)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""{SQL_AREA}
                SELECT ST_IsEmpty(a.g) AS vazia,
                       ST_Area(a.g::geography) / 10000.0 AS ha,
                       ST_X(ST_PointOnSurface(a.g)) AS lon,
                       ST_Y(ST_PointOnSurface(a.g)) AS lat,
                       ST_AsGeoJSON(a.g, 6) AS geom_json,
                       ST_XMin(a.g) AS x0, ST_YMin(a.g) AS y0, ST_XMax(a.g) AS x1, ST_YMax(a.g) AS y1
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

        def adicionar(id_: str, titulo: str, base: str, tabela: str, itens: list[dict], status_se_houver: str,
                      fonte: str | None = None):
            """itens: [{descricao, ha, geoms?, status?}]. `fonte` exige aquela carga (tabelas com várias fontes)."""
            carregada = tabela in bases and (fonte is None or any(b["fonte"] == fonte for b in bases[tabela]))
            if not carregada:
                checks.append({"id": id_, "titulo": titulo, "base": base, "status": "warn", "ha": 0,
                               "itens": [{"descricao": "Base ainda não carregada no sistema. Verificação não realizada.", "ha": None}]})
                return
            for i in itens:
                st = i.pop("status", status_se_houver)
                for gj in i.pop("geoms", []):
                    conflitos.append({"type": "Feature", "geometry": json.loads(gj),
                                      "properties": {"status": st, "rotulo": titulo}})
            checks.append({
                "id": id_, "titulo": titulo, "base": base,
                "status": status_se_houver if itens else "ok",
                "ha": round(sum(i["ha"] or 0 for i in itens), 2),
                "itens": itens or [{"descricao": "Nenhuma sobreposição encontrada.", "ha": None}],
            })

        # 1-3. Desmatamento (PRODES), agrupado por ano
        grupos: dict[str, dict[int | None, dict]] = {"pos_2020": {}, "pos_2008": {}, "consolidado": {}}
        prodes = _sobreposicoes(cur, "geo.desmatamento", "t.ano, t.data_imagem, t.fonte", g)
        for r in prodes:
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

        # Alertas recentes de desmatamento (MapBiomas Alerta e DETER), posteriores ao corte do EUDR
        alertas_todos = _sobreposicoes(cur, "geo.alertas_desmatamento", "t.fonte, t.data_deteccao, t.id_origem", g)
        alertas = [r for r in alertas_todos if regras.alerta_pos_corte(r["data_deteccao"])]
        fontes_carregadas = {b["fonte"] for lst in bases.values() for b in lst}

        def itens_alerta(fonte, nome):
            por_ano: dict[int | None, dict] = {}
            for r in alertas:
                if r["fonte"] != fonte:
                    continue
                ano = r["data_deteccao"].year if r["data_deteccao"] else None
                gr = por_ano.setdefault(ano, {"n": 0, "ha": 0.0, "geoms": [], "datas": []})
                gr["n"] += 1
                gr["ha"] += r["ha"]
                gr["geoms"].append(r["geom_json"])
                if r["data_deteccao"]:
                    gr["datas"].append(r["data_deteccao"])
            return [{"descricao": f"{nome} {ano or 'sem data'}: {gr['n']} alerta(s)"
                                  + (f", o mais recente detectado em {_data_br(max(gr['datas']))}" if gr["datas"] else ""),
                     "ha": round(gr["ha"], 2), "geoms": gr["geoms"]}
                    for ano, gr in sorted(por_ano.items(), key=lambda kv: kv[0] or 0, reverse=True)]

        if "mapbiomas_alerta" in fontes_carregadas:  # base opcional: só entra no laudo se estiver configurada
            adicionar("alertas_mapbiomas", "Alertas validados de desmatamento após 31/12/2020",
                      "MapBiomas Alerta: validados por analistas com imagem de alta resolução", "geo.alertas_desmatamento",
                      itens_alerta("mapbiomas_alerta", "MapBiomas"), regras.STATUS_ALERTA["mapbiomas_alerta"],
                      fonte="mapbiomas_alerta")
        adicionar("alertas_deter", "Alertas recentes de desmatamento (DETER)",
                  "INPE: alerta rápido, ainda sem confirmação do PRODES ou de imagem", "geo.alertas_desmatamento",
                  itens_alerta("deter_cerrado", "DETER"), regras.STATUS_ALERTA["deter_cerrado"], fonte="deter_cerrado")

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

        # 8-9. Territórios quilombolas e assentamentos (INCRA)
        ter = _sobreposicoes(cur, "geo.territorios", "t.tipo, t.nome, t.municipio, t.uf, t.fase, t.familias", g)

        def desc_territorio(r):
            onde = f", {r['municipio'].title()}/{r['uf']}" if r["municipio"] and r["uf"] else ""
            familias = f", {r['familias']} famílias" if r["familias"] else ""
            return f"{r['nome'] or 'sem nome'} ({r['fase'] or 'fase não informada'}{onde}{familias})"

        quilombos = [r for r in ter if r["tipo"] == "quilombola"]
        st_quil = max((regras.status_quilombo(r["fase"]) for r in quilombos), key=lambda s: regras.ORDEM[s],
                      default="bad")
        adicionar("quilombolas", "Territórios quilombolas",
                  "INCRA. Reconhecido (portaria, decreto ou título): Não conforme; em estudo: Atenção",
                  "geo.territorios", [
                      {"descricao": desc_territorio(r), "ha": round(r["ha"], 2), "geoms": [r["geom_json"]],
                       "status": regras.status_quilombo(r["fase"])}
                      for r in quilombos
                  ], st_quil, fonte="quilombolas_incra")
        adicionar("assentamentos", "Assentamentos da reforma agrária",
                  "INCRA. Área pública destinada à reforma agrária: exige análise da regularidade",
                  "geo.territorios", [
                      {"descricao": desc_territorio(r), "ha": round(r["ha"], 2), "geoms": [r["geom_json"]]}
                      for r in ter if r["tipo"] == "assentamento"
                  ], "warn", fonte="assentamentos_incra")

        # 10. Reserva Legal
        rl = reserva_legal_ha if reserva_legal_ha is not None else (imovel or {}).get("reserva_legal_ha")
        st_rl, texto_rl = regras.avaliar_reserva_legal(rl, area["ha"])
        checks.append({"id": "reserva_legal", "titulo": "Reserva Legal (mínimo 20%)",
                       "base": "Art. 12, II, da Lei 12.651/2012 (Cerrado fora da Amazônia Legal)",
                       "status": st_rl, "ha": rl or 0, "itens": [{"descricao": texto_rl, "ha": None}]})

        # Elegibilidade RenovaBio (fora do resultado geral). Supressão = PRODES ou alerta posterior ao corte;
        # sobreposições entre bases são unidas no PostGIS para não contar a mesma área duas vezes.
        if "geo.desmatamento" in bases:
            geoms = ([r["geom_json"] for r in prodes if regras.supressao_pos_renovabio(r["ano"], r["data_imagem"])]
                     + [r["geom_json"] for r in alertas_todos
                        if r["fonte"] in fontes_carregadas and r["data_deteccao"]
                        and r["data_deteccao"] > regras.CORTE_RENOVABIO])
            ha_rb = 0.0
            if geoms:
                cur.execute("""SELECT ST_Area(ST_Union(ST_MakeValid(ST_GeomFromGeoJSON(x)))::geography) / 10000.0 AS ha
                               FROM unnest(%s::text[]) AS x""", (geoms,))
                ha_rb = cur.fetchone()["ha"] or 0.0
            st_rb, rot_rb, txt_rb = regras.avaliar_renovabio(
                ha_rb, (imovel or {}).get("situacao"), (imovel or {}).get("tipo"), AREA_MINIMA_HA)
            checks.append({"id": "renovabio", "titulo": "Elegibilidade RenovaBio (biomassa)",
                           "base": f"Resolução ANP 984/2025: sem supressão de vegetação nativa desde "
                                   f"{regras.CORTE_RENOVABIO:%d/%m/%Y} e CAR ativo ou pendente. Não altera o resultado geral",
                           "status": st_rb, "rotulo": rot_rb, "ha": round(ha_rb, 2),
                           "itens": [{"descricao": txt_rb, "ha": None}]})

        # 11. Imóveis sobrepostos (identificação, não pesa no resultado)
        imv = _sobreposicoes(cur, "geo.imoveis", "t.id, t.tipo, t.codigo, t.nome", g)
        if "geo.imoveis" in bases:
            checks.append({
                "id": "imoveis", "titulo": "Imóveis cadastrados sobrepostos", "base": "SIGEF (INCRA) e CAR (SICAR)",
                "status": "info" if imv else "ok", "ha": round(sum(r["ha"] for r in imv), 2),
                "itens": [{"descricao": f"{r['tipo']} {r['codigo'] or ''} {('· ' + r['nome']) if r['nome'] else ''}".strip(),
                           "ha": round(r["ha"], 2)} for r in sorted(imv, key=lambda r: -r["ha"])[:15]]
                         or [{"descricao": "Nenhum imóvel cadastrado sobreposto.", "ha": None}],
            })

        geral = regras.status_geral([c["status"] for c in checks if regras.entra_no_resultado(c)])
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
        # Imagens de satélite: os metadados (data, cenas, SHA-256 do PNG) entram no código de integridade
        pngs: dict[str, bytes] = {}
        if IMAGENS_SATELITE and com_imagens:
            resultado["imagens"] = []
            for meta, png in imagens.imagens_antes_depois((area["x0"], area["y0"], area["x1"], area["y1"]),
                                                          agora.astimezone(FUSO_BAHIA).date()):
                resultado["imagens"].append(meta)
                if png:
                    pngs[meta["tipo"]] = png
        # Gravado junto e impresso no PDF: qualquer alteração posterior no conteúdo muda o código
        resultado["integridade"] = codigo_integridade(resultado)

        if not gravar:  # consulta pública: resultado na hora, sem protocolo verificável
            conn.rollback()
            return resultado
        cur.execute(
            """INSERT INTO geo.analises (protocolo, nome_area, imovel_id, area_ha, resultado, detalhes, geom,
                                         empresa_id, usuario_id, area_id)
               VALUES (%s, %s, %s, %s, %s, %s, geo.normalizar(ST_GeomFromGeoJSON(%s)), %s, %s, %s)""",
            (protocolo, resultado["area"]["nome"], (imovel or {}).get("id"), area["ha"], geral,
             Jsonb(resultado), area["geom_json"], empresa_id, usuario_id, area_id),
        )
        for tipo, png in pngs.items():
            cur.execute("INSERT INTO geo.analises_imagens (protocolo, tipo, png) VALUES (%s, %s, %s)",
                        (protocolo, tipo, png))
    conn.commit()
    return resultado
