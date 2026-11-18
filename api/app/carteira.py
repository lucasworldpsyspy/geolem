"""Carteira: as áreas (fornecedores) que cada empresa monitora.

Cadastrar uma área coloca uma tarefa na fila; o worker (`python -m app.worker`) faz a análise, grava o protocolo
na área e, se a situação piorar em relação à análise anterior, cria um alerta.
"""
import json

from . import licencas, regras
from .analise import ErroAnalise, analisar
from .config import AREA_MAXIMA_HA
from .imoveis import carregar_imovel, imovel_por_codigo

MAX_AREAS_POR_LOTE = 5000


class ErroCarteira(ValueError):
    pass


def adicionar_area(conn, empresa_id: int, usuario_id: int | None, *, nome: str | None, geometria: dict | None = None,
                   imovel_id: int | None = None, fornecedor: str | None = None, codigo_externo: str | None = None,
                   reserva_legal_ha: float | None = None, origem: str | None = None) -> int:
    """Cadastra a área e põe a primeira análise na fila. Usa a geometria do imóvel se só vier o id."""
    municipio = None
    if imovel_id is not None:
        im = carregar_imovel(conn, imovel_id)
        if not im:
            raise ErroCarteira("Imóvel não encontrado.")
        geometria = geometria or json.loads(im["geom_json"])
        nome = nome or im["nome"] or f"{im['tipo']} {im['codigo']}"
        municipio = f"{im['municipio']}/{im['uf']}" if im["municipio"] and im["uf"] else im["municipio"]
        if reserva_legal_ha is None:
            reserva_legal_ha = im["reserva_legal_ha"]
    if not geometria:
        raise ErroCarteira("Informe a geometria da área ou o imóvel.")
    try:
        licencas.exigir(conn, empresa_id, "areas")
    except licencas.ErroLicenca as e:
        raise ErroCarteira(str(e)) from e
    if geometria.get("type") == "Feature":
        geometria = geometria.get("geometry") or {}
    if geometria.get("type") not in ("Polygon", "MultiPolygon"):
        raise ErroCarteira("A geometria precisa ser um polígono.")

    linha = conn.execute(
        """WITH g AS (SELECT geo.normalizar(ST_GeomFromGeoJSON(%(g)s)) AS g)
           SELECT ST_IsEmpty(g) AS vazia, ST_Area(g::geography) / 10000.0 AS ha FROM g""",
        {"g": json.dumps(geometria)},
    ).fetchone()
    if linha["vazia"] or not linha["ha"]:
        raise ErroCarteira("A geometria não forma uma área válida.")
    if linha["ha"] > AREA_MAXIMA_HA:
        raise ErroCarteira(f"Área grande demais ({regras.fmt_ha(linha['ha'])}).")

    area_id = conn.execute(
        """INSERT INTO app.areas (empresa_id, nome, fornecedor, codigo_externo, imovel_id, municipio, reserva_legal_ha,
                                  origem, area_ha, geom, criado_por)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, geo.normalizar(ST_GeomFromGeoJSON(%s)), %s) RETURNING id""",
        (empresa_id, (nome or "Área sem nome")[:200], (fornecedor or None) and fornecedor[:200],
         (codigo_externo or None) and codigo_externo[:100], imovel_id, municipio, reserva_legal_ha, origem,
         linha["ha"], json.dumps(geometria), usuario_id),
    ).fetchone()["id"]
    enfileirar(conn, empresa_id, [area_id], usuario_id, "cadastro")
    return area_id


def importar_planilha(conn, empresa_id: int, usuario_id: int, linhas: list[dict], origem: str) -> dict:
    importadas, ignoradas = 0, []
    for l in linhas[:MAX_AREAS_POR_LOTE]:
        if l.get("erro"):
            ignoradas.append({"linha": l["linha"], "motivo": l["erro"]})
            continue
        if not l.get("codigo"):
            ignoradas.append({"linha": l["linha"], "motivo": "Sem código do imóvel."})
            continue
        im = imovel_por_codigo(conn, l["codigo"])
        if not im:
            ignoradas.append({"linha": l["linha"], "motivo": f"Código {l['codigo']} não encontrado nas bases (SIGEF/CAR)."})
            continue
        try:
            with conn.transaction():
                adicionar_area(conn, empresa_id, usuario_id, nome=l.get("nome"), imovel_id=im["id"],
                               fornecedor=l.get("fornecedor"), codigo_externo=l.get("codigo_externo"),
                               reserva_legal_ha=l.get("reserva_legal_ha"), origem=origem)
            importadas += 1
        except ErroCarteira as e:
            ignoradas.append({"linha": l["linha"], "motivo": str(e)})
    return {"importadas": importadas, "ignoradas": ignoradas}


def importar_feicoes(conn, empresa_id: int, usuario_id: int, feicoes: list[dict], origem: str) -> dict:
    importadas, ignoradas = 0, []
    for f in feicoes[:MAX_AREAS_POR_LOTE]:
        try:
            with conn.transaction():
                adicionar_area(conn, empresa_id, usuario_id, nome=f["nome"], geometria=f["geometria"],
                               fornecedor=f.get("fornecedor"), codigo_externo=f.get("codigo_externo"), origem=origem)
            importadas += 1
        except (ErroCarteira, Exception) as e:  # geometria inválida não derruba o lote
            ignoradas.append({"linha": f["linha"], "motivo": str(e)[:200]})
    return {"importadas": importadas, "ignoradas": ignoradas}


def enfileirar(conn, empresa_id: int, area_ids: list[int], usuario_id: int | None, motivo: str) -> int:
    """Põe as áreas na fila, sem duplicar as que já estão esperando."""
    if not area_ids:
        return 0
    return conn.execute(
        """INSERT INTO app.tarefas (empresa_id, area_id, usuario_id, motivo)
           SELECT a.empresa_id, a.id, %s, %s FROM app.areas a
           WHERE a.id = ANY(%s) AND a.empresa_id = %s AND a.ativa
             AND NOT EXISTS (SELECT 1 FROM app.tarefas t WHERE t.area_id = a.id AND t.estado IN ('pendente', 'executando'))""",
        (usuario_id, motivo, area_ids, empresa_id),
    ).rowcount


def enfileirar_monitoramento(conn) -> int:
    """Reanálise de todas as áreas ativas de todas as empresas (rodar depois da carga semanal das bases)."""
    return conn.execute(
        """INSERT INTO app.tarefas (empresa_id, area_id, motivo)
           SELECT a.empresa_id, a.id, 'monitoramento' FROM app.areas a JOIN app.empresas e ON e.id = a.empresa_id
           WHERE a.ativa AND e.ativa
             AND NOT EXISTS (SELECT 1 FROM app.tarefas t WHERE t.area_id = a.id AND t.estado IN ('pendente', 'executando'))"""
    ).rowcount


def _resumo_alerta(resultado: dict) -> str:
    problemas = [f"{c['titulo']} ({regras.fmt_ha(c['ha'])})" if c.get("ha") else c["titulo"]
                 for c in resultado["checks"] if c["status"] in ("bad", "warn") and regras.entra_no_resultado(c)]
    return "; ".join(problemas)[:500] or resultado["resumo"]


def processar_tarefa(conn, tarefa: dict) -> str:
    """Analisa a área da tarefa e atualiza a carteira. Devolve o protocolo."""
    area = conn.execute(
        """SELECT id, empresa_id, nome, imovel_id, reserva_legal_ha, ultimo_status, ST_AsGeoJSON(geom, 6) AS g
           FROM app.areas WHERE id = %s""", (tarefa["area_id"],)).fetchone()
    if not area:
        raise ErroCarteira("Área removida.")
    imovel = None
    if area["imovel_id"]:
        imovel = carregar_imovel(conn, area["imovel_id"])
        if imovel:
            imovel.pop("geom_json", None)
    try:
        r = analisar(conn, json.loads(area["g"]), nome=area["nome"], imovel=imovel,
                     reserva_legal_ha=area["reserva_legal_ha"], empresa_id=area["empresa_id"],
                     usuario_id=tarefa.get("usuario_id"), area_id=area["id"])
    except ErroAnalise as e:
        conn.rollback()
        raise ErroCarteira(str(e)) from e

    anterior, novo = area["ultimo_status"], r["geral"]
    conn.execute(
        """UPDATE app.areas SET ultimo_protocolo = %s, status_anterior = ultimo_status, ultimo_status = %s,
                                analisado_em = now() WHERE id = %s""",
        (r["protocolo"], novo, area["id"]))
    # Alerta só quando piora (ex.: Conforme -> Atenção, Atenção -> Não conforme) numa reanálise
    if anterior and regras.ORDEM[novo] > regras.ORDEM[anterior]:
        conn.execute(
            """INSERT INTO app.alertas (empresa_id, area_id, de, para, protocolo, resumo)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (area["empresa_id"], area["id"], anterior, novo, r["protocolo"], _resumo_alerta(r)))
    conn.commit()
    return r["protocolo"]


def resumo_carteira(conn, empresa_id: int) -> dict:
    t = conn.execute(
        """SELECT count(*) AS areas, coalesce(sum(area_ha), 0) AS area_ha,
                  count(*) FILTER (WHERE ultimo_status = 'ok') AS ok,
                  count(*) FILTER (WHERE ultimo_status = 'warn') AS warn,
                  count(*) FILTER (WHERE ultimo_status = 'bad') AS bad,
                  count(*) FILTER (WHERE ultimo_status IS NULL) AS sem_analise,
                  coalesce(sum(area_ha) FILTER (WHERE ultimo_status = 'bad'), 0) AS area_bad_ha,
                  max(analisado_em) AS ultima_analise
           FROM app.areas WHERE empresa_id = %s AND ativa""", (empresa_id,)).fetchone()
    fila = conn.execute(
        """SELECT count(*) FILTER (WHERE estado IN ('pendente', 'executando')) AS na_fila,
                  count(*) FILTER (WHERE estado = 'erro' AND concluido_em > now() - interval '7 days') AS erros
           FROM app.tarefas WHERE empresa_id = %s""", (empresa_id,)).fetchone()
    alertas = conn.execute("SELECT count(*) AS n FROM app.alertas WHERE empresa_id = %s AND lido_em IS NULL",
                           (empresa_id,)).fetchone()["n"]
    return {**t, "ultima_analise": t["ultima_analise"].isoformat() if t["ultima_analise"] else None,
            "na_fila": fila["na_fila"], "erros_recentes": fila["erros"], "alertas_nao_lidos": alertas}


def listar_areas(conn, empresa_id: int) -> list[dict]:
    linhas = conn.execute(
        """SELECT a.id, a.nome, a.fornecedor, a.codigo_externo, a.municipio, round(a.area_ha::numeric, 2) AS area_ha,
                  a.ultimo_status, a.status_anterior, a.ultimo_protocolo, a.analisado_em, a.origem, a.criado_em,
                  t.estado AS fila, t.erro AS erro,
                  CASE WHEN cv.aceito_em IS NOT NULL THEN 'aceito' WHEN cv.id IS NOT NULL THEN 'enviado' END AS convite
           FROM app.areas a
           LEFT JOIN LATERAL (SELECT estado, erro FROM app.tarefas WHERE area_id = a.id
                              ORDER BY id DESC LIMIT 1) t ON true
           LEFT JOIN LATERAL (SELECT id, aceito_em FROM app.convites WHERE area_id = a.id
                              ORDER BY aceito_em IS NULL, id DESC LIMIT 1) cv ON true
           WHERE a.empresa_id = %s AND a.ativa
           ORDER BY CASE a.ultimo_status WHEN 'bad' THEN 0 WHEN 'warn' THEN 1 WHEN 'ok' THEN 3 ELSE 2 END, a.nome""",
        (empresa_id,)).fetchall()
    for l in linhas:
        for k in ("analisado_em", "criado_em"):
            l[k] = l[k].isoformat() if l[k] else None
        l["area_ha"] = float(l["area_ha"]) if l["area_ha"] is not None else None
        if l["fila"] not in ("erro",):
            l["erro"] = None
    return linhas
