"""Observatório público do oeste baiano: números agregados da região (nunca dados de um imóvel identificado).

Os números vêm das bases já carregadas e ficam em cache por algumas horas: a página abre na hora e mostra a data
de referência. O cálculo completo leva poucos segundos.
"""
import json
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import REGIAO_BBOX, URL_PUBLICA

CACHE_SEGUNDOS = 6 * 3600
_cache: dict = {"em": 0.0, "dados": None}
_trava = threading.Lock()
MIN_IMOVEIS_MUNICIPIO = 15  # municípios com poucos imóveis na base ficam fora do ranking (número pouco confiável)


def chave_municipio(nome: str | None) -> str:
    t = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    return " ".join(t.upper().replace("/", " ").split())


def _municipios(conn) -> tuple[list[dict], dict]:
    """Indicadores por município pela malha oficial do IBGE (cruzamento espacial) e a produção da PAM.
    Devolve (lista de municípios, GeoJSON do mapa)."""
    linhas = conn.execute(
        """WITH m AS (SELECT id_origem AS cod, nome, uf, area_km2, geom FROM geo.municipios),
           im AS (
             SELECT i.geom, i.area_ha,
                    EXISTS (SELECT 1 FROM geo.desmatamento d WHERE d.fonte = 'prodes_cerrado' AND d.ano >= 2021
                              AND ST_Intersects(d.geom, i.geom)
                              AND ST_Area(ST_Intersection(d.geom, i.geom)::geography) > 10000) AS pos2020,
                    EXISTS (SELECT 1 FROM geo.embargos e WHERE ST_Intersects(e.geom, i.geom)
                              AND ST_Area(ST_Intersection(e.geom, i.geom)::geography) > 10000) AS embargo
             FROM geo.imoveis i WHERE i.tipo = 'SIGEF')
           SELECT m.cod, m.nome, m.uf, m.area_km2,
                  ST_Area(ST_Intersection(m.geom, ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326)))
                    / nullif(ST_Area(m.geom), 0) AS fracao_monitorada,
                  (SELECT count(*) FROM im WHERE ST_Contains(m.geom, ST_PointOnSurface(im.geom))) AS imoveis,
                  (SELECT coalesce(sum(im.area_ha), 0) FROM im WHERE ST_Contains(m.geom, ST_PointOnSurface(im.geom))) AS ha,
                  (SELECT count(*) FROM im WHERE im.pos2020 AND ST_Contains(m.geom, ST_PointOnSurface(im.geom))) AS pos2020,
                  (SELECT count(*) FROM im WHERE im.embargo AND ST_Contains(m.geom, ST_PointOnSurface(im.geom))) AS embargo,
                  (SELECT coalesce(sum(ST_Area(ST_Intersection(d.geom, m.geom)::geography)), 0) / 10000
                     FROM geo.desmatamento d WHERE d.fonte = 'prodes_cerrado' AND d.ano >= 2021
                     AND ST_Intersects(d.geom, m.geom)) AS ha_desmat_pos2020,
                  (SELECT count(*) FROM geo.alertas_desmatamento a WHERE a.fonte = 'deter_cerrado'
                     AND a.data_deteccao >= now() - interval '12 months' AND ST_Intersects(a.geom, m.geom)) AS deter_alertas,
                  (SELECT coalesce(sum(ST_Area(ST_Intersection(a.geom, m.geom)::geography)), 0) / 10000
                     FROM geo.alertas_desmatamento a WHERE a.fonte = 'deter_cerrado'
                     AND a.data_deteccao >= now() - interval '12 months' AND ST_Intersects(a.geom, m.geom)) AS deter_ha,
                  (SELECT count(*) FROM geo.embargos e WHERE ST_Intersects(e.geom, m.geom)) AS embargos,
                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(m.geom, 0.005), 4) AS g
           FROM m""", dict(zip(("x0", "y0", "x1", "y1"), REGIAO_BBOX))).fetchall()
    pam = conn.execute(
        """SELECT cod_ibge, produto, valor_mil_reais, quantidade_t, area_colhida_ha, rendimento_kg_ha, ano
           FROM geo.producao_municipal WHERE ano = (SELECT max(ano) FROM geo.producao_municipal)""").fetchall()
    por_mun: dict[str, dict] = {}
    for p in pam:
        por_mun.setdefault(p["cod_ibge"], {})[p["produto"]] = p
    # Preço médio regional da soja na PAM (R$/kg), para estimar o valor da produção nas áreas abertas após 2020
    soja = [p for p in pam if p["produto"] == "soja" and p["quantidade_t"] and p["valor_mil_reais"]]
    reais_kg_soja = (sum(p["valor_mil_reais"] for p in soja) * 1000 / (sum(p["quantidade_t"] for p in soja) * 1000)
                     if soja else None)
    saida, feicoes = [], []
    for l in linhas:
        prod = por_mun.get(l["cod"], {})
        vbp = {k: round((v["valor_mil_reais"] or 0) * 1000) for k, v in prod.items() if v["valor_mil_reais"]}
        rend_soja = (prod.get("soja") or {}).get("rendimento_kg_ha")
        valor_risco = (float(l["ha_desmat_pos2020"]) * rend_soja * reais_kg_soja
                       if rend_soja and reais_kg_soja else None)
        m = {
            "cod_ibge": l["cod"], "municipio": l["nome"], "uf": l["uf"], "area_km2": l["area_km2"],
            "imoveis": l["imoveis"], "ha": round(float(l["ha"])), "pos2020": l["pos2020"], "embargo": l["embargo"],
            "pct_pos2020": round(100 * l["pos2020"] / l["imoveis"], 1) if l["imoveis"] else 0,
            "ha_desmat_pos2020": round(float(l["ha_desmat_pos2020"])),
            "deter_alertas": l["deter_alertas"], "deter_ha": round(float(l["deter_ha"])), "embargos": l["embargos"],
            "vbp_total": vbp.get("total"), "vbp_culturas": {k: v for k, v in vbp.items() if k != "total"},
            "valor_risco_eudr": round(valor_risco) if valor_risco else None,
            # Bases de desmatamento e imóveis são carregadas na região monitorada; a PAM vale para o município todo
            "pct_monitorado": round(100 * float(l["fracao_monitorada"] or 0)),
        }
        saida.append(m)
        feicoes.append({"type": "Feature", "geometry": json.loads(l["g"]),
                        "properties": {k: v for k, v in m.items() if k != "vbp_culturas"}})
    saida.sort(key=lambda m: -(m["vbp_total"] or 0))
    return saida, {"type": "FeatureCollection", "features": feicoes}


def _calcular(conn) -> dict:
    # Imóveis SIGEF com desmatamento PRODES após 2020 (sobreposição > 1 ha) e com embargo
    imoveis = conn.execute(
        """WITH i AS (
             SELECT i.id, i.municipio, i.uf, i.area_ha,
                    EXISTS (SELECT 1 FROM geo.desmatamento d WHERE d.fonte = 'prodes_cerrado' AND d.ano >= 2021
                              AND ST_Intersects(d.geom, i.geom)
                              AND ST_Area(ST_Intersection(d.geom, i.geom)::geography) > 10000) AS pos2020,
                    EXISTS (SELECT 1 FROM geo.embargos e WHERE ST_Intersects(e.geom, i.geom)
                              AND ST_Area(ST_Intersection(e.geom, i.geom)::geography) > 10000) AS embargo
             FROM geo.imoveis i WHERE i.tipo = 'SIGEF')
           SELECT municipio, uf, count(*) AS imoveis, round(sum(area_ha)) AS ha,
                  count(*) FILTER (WHERE pos2020) AS pos2020, count(*) FILTER (WHERE embargo) AS embargo
           FROM i GROUP BY municipio, uf""").fetchall()
    total_imoveis = sum(r["imoveis"] for r in imoveis)
    total_pos2020 = sum(r["pos2020"] for r in imoveis)

    por_ano = conn.execute(
        """SELECT ano, round(sum(area_km2) * 100) AS ha FROM geo.desmatamento
           WHERE fonte = 'prodes_cerrado' AND ano BETWEEN 2008 AND extract(year FROM now())::int
           GROUP BY ano ORDER BY ano""").fetchall()
    deter_mes = conn.execute(
        """SELECT to_char(date_trunc('month', data_deteccao), 'YYYY-MM') AS mes, count(*) AS alertas,
                  round(sum(area_ha)) AS ha
           FROM geo.alertas_desmatamento
           WHERE fonte = 'deter_cerrado' AND data_deteccao >= date_trunc('month', now()) - interval '23 months'
           GROUP BY 1 ORDER BY 1""").fetchall()
    deter_mun = conn.execute(
        """SELECT municipio, uf, count(*) AS alertas, round(sum(area_ha)) AS ha FROM geo.alertas_desmatamento
           WHERE fonte = 'deter_cerrado' AND data_deteccao >= now() - interval '12 months' AND municipio IS NOT NULL
           GROUP BY 1, 2""").fetchall()
    recentes = conn.execute(
        """SELECT count(*) FILTER (WHERE data_deteccao >= now() - interval '30 days') AS alertas_30d,
                  round(coalesce(sum(area_ha) FILTER (WHERE data_deteccao >= now() - interval '30 days'), 0)) AS ha_30d,
                  count(*) FILTER (WHERE data_deteccao >= now() - interval '12 months') AS alertas_12m,
                  round(coalesce(sum(area_ha) FILTER (WHERE data_deteccao >= now() - interval '12 months'), 0)) AS ha_12m,
                  max(data_deteccao) AS ultimo
           FROM geo.alertas_desmatamento WHERE fonte = 'deter_cerrado'""").fetchone()
    embargos_mun = conn.execute(
        "SELECT municipio, uf, count(*) AS embargos FROM geo.embargos GROUP BY 1, 2").fetchall()
    outros = conn.execute(
        """SELECT (SELECT count(*) FROM geo.embargos) AS embargos,
                  (SELECT count(*) FROM geo.territorios WHERE tipo = 'assentamento') AS assentamentos,
                  (SELECT count(*) FROM geo.unidades_conservacao) AS ucs,
                  (SELECT max(concluido_em) FROM geo.cargas WHERE status = 'ok') AS atualizado_em""").fetchone()

    # Junta as fontes pelo nome do município (sem acento e em maiúsculas)
    mun: dict[str, dict] = {}
    for r in imoveis:
        if not r["municipio"] or r["municipio"].startswith("IBGE "):
            continue
        m = mun.setdefault(chave_municipio(r["municipio"]), {"municipio": r["municipio"], "uf": r["uf"], "imoveis": 0,
                                                             "ha": 0, "pos2020": 0, "embargo": 0, "deter_alertas": 0,
                                                             "deter_ha": 0, "embargos": 0})
        m["imoveis"] += r["imoveis"]; m["ha"] += float(r["ha"] or 0)
        m["pos2020"] += r["pos2020"]; m["embargo"] += r["embargo"]
    for r in deter_mun:
        m = mun.get(chave_municipio(r["municipio"]))
        if m:
            m["deter_alertas"] += r["alertas"]; m["deter_ha"] += float(r["ha"] or 0)
    for r in embargos_mun:
        m = mun.get(chave_municipio(r["municipio"]))
        if m:
            m["embargos"] += r["embargos"]
    ranking = sorted((dict(m, pct_pos2020=round(100 * m["pos2020"] / m["imoveis"], 1))
                      for m in mun.values() if m["imoveis"] >= MIN_IMOVEIS_MUNICIPIO),
                     key=lambda m: -m["pos2020"])

    # Com a malha do IBGE carregada, os municípios vêm do cruzamento espacial (mais preciso que casar nomes)
    mapa = None
    financeiro = None
    if conn.execute("SELECT 1 FROM geo.municipios LIMIT 1").fetchone():
        municipios, mapa = _municipios(conn)
        ranking = sorted((m for m in municipios if m["imoveis"] >= MIN_IMOVEIS_MUNICIPIO), key=lambda m: -m["pos2020"])
        culturas: dict[str, int] = {}
        for m in municipios:
            for k, v in m["vbp_culturas"].items():
                culturas[k] = culturas.get(k, 0) + v
        ano_pam = conn.execute("SELECT max(ano) AS a FROM geo.producao_municipal").fetchone()["a"]
        financeiro = {
            "ano_pam": ano_pam,
            "vbp_total": sum(m["vbp_total"] or 0 for m in municipios),
            "vbp_por_cultura": dict(sorted(culturas.items(), key=lambda kv: -kv[1])),
            "ha_desmat_pos2020": sum(m["ha_desmat_pos2020"] for m in municipios),
            "valor_risco_eudr": sum(m["valor_risco_eudr"] or 0 for m in municipios),
            "municipios": [{k: m[k] for k in ("municipio", "uf", "vbp_total", "vbp_culturas", "ha_desmat_pos2020",
                                               "valor_risco_eudr", "deter_ha")} for m in municipios if m["vbp_total"]],
        }
    _cache["mapa"] = mapa
    return {
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "financeiro": financeiro,
        "atualizado_em": outros["atualizado_em"].isoformat() if outros["atualizado_em"] else None,
        "totais": {
            "imoveis": total_imoveis, "ha": round(sum(float(r["ha"] or 0) for r in imoveis)),
            "imoveis_pos2020": total_pos2020,
            "pct_pos2020": round(100 * total_pos2020 / total_imoveis, 1) if total_imoveis else 0,
            "imoveis_embargo": sum(r["embargo"] for r in imoveis),
            "embargos": outros["embargos"], "assentamentos": outros["assentamentos"], "ucs": outros["ucs"],
            "deter_alertas_30d": recentes["alertas_30d"], "deter_ha_30d": float(recentes["ha_30d"] or 0),
            "deter_alertas_12m": recentes["alertas_12m"], "deter_ha_12m": float(recentes["ha_12m"] or 0),
            "deter_ultimo": recentes["ultimo"].isoformat() if recentes["ultimo"] else None,
        },
        "prodes_por_ano": [{"ano": r["ano"], "ha": float(r["ha"])} for r in por_ano],
        "deter_por_mes": [{"mes": r["mes"], "alertas": r["alertas"], "ha": float(r["ha"] or 0)} for r in deter_mes],
        "municipios": ranking,
    }


def aquecer(pool) -> None:
    """Calcula o cache em segundo plano ao subir a API, para o primeiro visitante não esperar."""
    def tarefa():
        try:
            with pool.connection() as conn:
                dados(conn, forcar=True)
        except Exception as e:  # sem bases carregadas ainda, por exemplo
            print(f"Observatório: cache não pré-calculado ({e})", flush=True)
    threading.Thread(target=tarefa, daemon=True).start()


def dados(conn, forcar: bool = False) -> dict:
    with _trava:
        if forcar or not _cache["dados"] or time.monotonic() - _cache["em"] > CACHE_SEGUNDOS:
            _cache["dados"] = _calcular(conn)
            _cache["em"] = time.monotonic()
        return _cache["dados"]


def mapa(conn) -> dict | None:
    dados(conn)  # garante o cálculo (o mapa é gerado junto)
    return _cache.get("mapa")


def municipio(conn, nome: str) -> dict | None:
    chave = chave_municipio(nome)
    return next((m for m in dados(conn)["municipios"] if chave_municipio(m["municipio"]) == chave), None)


def _n(v, casas=0) -> str:
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pdf_municipio(conn, nome: str) -> bytes | None:
    """Relatório de uma página com os números agregados do município (isca do observatório)."""
    m = municipio(conn, nome)
    if not m:
        return None
    d = dados(conn)
    verde = colors.HexColor("#1d7a52")
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=f"GeoLEM - {m['municipio']}", author="GeoLEM")
    from reportlab.lib.styles import ParagraphStyle
    t = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=18, leading=22)
    s = ParagraphStyle("s", fontName="Helvetica", fontSize=9.5, leading=13, textColor=colors.HexColor("#5b6660"))
    c = ParagraphStyle("c", fontName="Helvetica", fontSize=10, leading=14)
    h = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=11, leading=15, textColor=verde, spaceBefore=10)
    ref = (datetime.fromisoformat(d["atualizado_em"]).strftime("%d/%m/%Y") if d["atualizado_em"] else "-")
    linhas = [
        ["Imóveis rurais certificados (SIGEF)", _n(m["imoveis"]), f"{_n(m['ha'])} ha"],
        ["Imóveis com desmatamento após 31/12/2020 (PRODES)", _n(m["pos2020"]), f"{_n(m['pct_pos2020'], 1)}% dos imóveis"],
        ["Imóveis sobrepostos a área embargada (IBAMA)", _n(m["embargo"]), ""],
        ["Áreas embargadas no município (IBAMA)", _n(m["embargos"]), ""],
        ["Alertas DETER nos últimos 12 meses", _n(m["deter_alertas"]), f"{_n(m['deter_ha'])} ha"],
    ]
    tab = Table(linhas, colWidths=[100 * mm, 30 * mm, 44 * mm])
    tab.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cfd8d3")),
                             ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                             ("FONT", (1, 0), (1, -1), "Helvetica-Bold", 10.5), ("TOPPADDING", (0, 0), (-1, -1), 6),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    h_list = [
        Paragraph('<font color="#1d7a52"><b>GeoLEM</b></font> · Observatório do Oeste Baiano', s), Spacer(1, 6),
        Paragraph(f"{m['municipio']}{('/' + m['uf']) if m['uf'] else ''}: compliance socioambiental em números", t),
        Paragraph(f"Bases oficiais atualizadas em {ref} · relatório gerado em {date.today().strftime('%d/%m/%Y')}", s),
        Spacer(1, 12), tab,
        Paragraph("O que isso significa", h),
        Paragraph(
            f"Em {_n(m['pct_pos2020'], 1)}% dos imóveis certificados do município há desmatamento detectado pelo PRODES "
            "depois de 31/12/2020, a data de corte do Regulamento Europeu antidesmatamento (EUDR). Grãos dessas áreas "
            "não podem ser exportados para a União Europeia, e o desmatamento também pesa na análise de crédito rural. "
            "Para quem compra, armazena ou financia a produção, a checagem precisa ser feita área por área, com "
            "registro do que foi verificado.", c),
        Paragraph("Como o GeoLEM ajuda", h),
        Paragraph(
            "O GeoLEM cruza cada imóvel com as bases oficiais (PRODES, DETER, MapBiomas Alerta, embargos, Unidades de "
            "Conservação, Terras Indígenas, quilombolas e assentamentos), mostra imagens de satélite de antes e depois "
            "de 2020 e emite um laudo com QR code de autenticidade. A carteira de fornecedores é monitorada toda semana, "
            "com alerta quando a situação de uma área piora.", c),
        Spacer(1, 14),
        Paragraph(f"Fale com a gente: {URL_PUBLICA}/observatorio.html", c),
        Spacer(1, 18),
        Paragraph("Números agregados de bases públicas (INCRA/SIGEF, INPE/PRODES e DETER, IBAMA). Imóveis com menos de 1 ha "
                  "de sobreposição não são contados. Não identifica imóveis nem pessoas e não substitui a análise de cada "
                  "área.", s),
    ]
    doc.build(h_list)
    return buf.getvalue()


def periodo_boletim(hoje: date | None = None) -> tuple[date, date]:
    fim = hoje or date.today()
    return fim - timedelta(days=7), fim
