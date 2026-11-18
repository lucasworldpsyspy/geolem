"""Laudo de conformidade em PDF, gerado no servidor a partir da análise gravada.

O PDF sai sempre do que está em geo.analises: o mesmo protocolo gera o mesmo laudo, e o QR code leva à página
pública de verificação, que mostra os mesmos dados e o mesmo código de integridade (SHA-256).
"""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from io import BytesIO

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import regras

FUSO_BAHIA = timezone(timedelta(hours=-3))  # a Bahia não tem horário de verão

VERDE = colors.HexColor("#1d7a52")
CINZA = colors.HexColor("#5b6660")
BORDA = colors.HexColor("#cfd8d3")
FUNDO = colors.HexColor("#f1f4f2")
CORES = {  # (texto, fundo)
    "ok": (colors.HexColor("#17663f"), colors.HexColor("#dff3e8")),
    "info": (colors.HexColor("#2f5f8a"), colors.HexColor("#e2edf7")),
    "warn": (colors.HexColor("#8a5a00"), colors.HexColor("#fdf0d5")),
    "bad": (colors.HexColor("#a3262b"), colors.HexColor("#fbdcdd")),
}
COR_CONFLITO = {"bad": colors.HexColor("#d64545"), "warn": colors.HexColor("#e0a020"),
                "info": colors.HexColor("#8a8f8c")}


def codigo_integridade(resultado: dict) -> str:
    """SHA-256 do conteúdo canônico da análise. Impresso no laudo e exibido na verificação."""
    sem_hash = {k: v for k, v in resultado.items() if k not in ("integridade", "integridade_conferida")}
    canonico = json.dumps(sem_hash, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode()).hexdigest()


def _data_hora(iso: str | None) -> str:
    if not iso:
        return "-"
    d = datetime.fromisoformat(iso)
    if d.tzinfo:
        d = d.astimezone(FUSO_BAHIA)
    return d.strftime("%d/%m/%Y %H:%M")


def _num(v: float | None, casas: int = 2) -> str:
    if v is None:
        return "-"
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------- estilos ----------
def _estilo(nome, **kw):
    base = dict(fontName="Helvetica", fontSize=9, leading=12, textColor=colors.black)
    base.update(kw)
    return ParagraphStyle(nome, **base)


E_TITULO = _estilo("titulo", fontName="Helvetica-Bold", fontSize=15, leading=18)
E_SUB = _estilo("sub", fontSize=8.5, textColor=CINZA)
E_DIR = _estilo("dir", fontSize=8.5, leading=11, alignment=TA_RIGHT)
E_H2 = _estilo("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=VERDE, spaceBefore=8, spaceAfter=4,
                keepWithNext=1)  # título nunca fica sozinho no pé da página
E_TXT = _estilo("txt")
E_PEQ = _estilo("peq", fontSize=7.8, leading=10)
E_CEL = _estilo("cel", fontSize=8, leading=10)
E_CEL_B = _estilo("celb", fontName="Helvetica-Bold", fontSize=8, leading=10)
E_AVISO = _estilo("aviso", fontSize=7.5, leading=9.5, textColor=CINZA)


def _esc(t) -> str:
    return (str(t) if t is not None else "-").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _selo(status: str) -> Paragraph:
    cor = CORES.get(status, CORES["info"])[0].hexval()[2:]
    return Paragraph(f'<font color="#{cor}"><b>{regras.ROTULOS.get(status, status)}</b></font>', E_CEL)


# ---------- mapa vetorial ----------
def _aneis(geom: dict) -> list[list[list[float]]]:
    """Lista de polígonos; cada polígono é uma lista de anéis (o primeiro é o externo)."""
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "Polygon":
        return [c]
    if t == "MultiPolygon":
        return list(c)
    if t == "GeometryCollection":
        return [p for g in geom.get("geometries", []) for p in _aneis(g)]
    return []


def _mapa(resultado: dict, largura: float, altura: float) -> Drawing:
    area = resultado["area"]["geometria"]
    conflitos = resultado.get("conflitos", {}).get("features", [])
    poligonos = _aneis(area)
    pontos = [p for pol in poligonos for anel in pol for p in anel]
    d = Drawing(largura, altura)
    d.add(Rect(0, 0, largura, altura, fillColor=colors.HexColor("#fbfcfb"), strokeColor=BORDA, strokeWidth=0.6))
    if not pontos:
        return d

    lon_min = min(p[0] for p in pontos); lon_max = max(p[0] for p in pontos)
    lat_min = min(p[1] for p in pontos); lat_max = max(p[1] for p in pontos)
    kx = math.cos(math.radians((lat_min + lat_max) / 2))  # equiretangular local: distâncias corretas na região
    margem = 10
    w_geo = max((lon_max - lon_min) * kx, 1e-9)
    h_geo = max(lat_max - lat_min, 1e-9)
    escala = min((largura - 2 * margem) / w_geo, (altura - 2 * margem - 10) / h_geo)
    ox = (largura - w_geo * escala) / 2
    oy = (altura - h_geo * escala) / 2 + 4

    def xy(p):
        return ox + (p[0] - lon_min) * kx * escala, oy + (p[1] - lat_min) * escala

    def desenhar(pols, preench, contorno, espessura):
        for pol in pols:
            for i, anel in enumerate(pol):
                if len(anel) < 3:
                    continue
                coords = [v for p in anel for v in xy(p)]
                fundo = preench if i == 0 else colors.HexColor("#fbfcfb")  # buracos
                d.add(Polygon(coords, fillColor=fundo, strokeColor=contorno, strokeWidth=espessura))

    desenhar(poligonos, colors.HexColor("#e6f2ec"), None, 0)
    ordem = {"info": 0, "warn": 1, "bad": 2}  # o mais grave por cima
    for f in sorted(conflitos, key=lambda f: ordem.get(f["properties"].get("status"), 0)):
        cor = COR_CONFLITO.get(f["properties"].get("status"), COR_CONFLITO["info"])
        desenhar(_aneis(f["geometry"]), colors.Color(cor.red, cor.green, cor.blue, alpha=0.55), cor, 0.4)
    desenhar([[pol[0]] for pol in poligonos], None, VERDE, 1.4)

    # Barra de escala com valor "redondo"
    metros_por_pt = 111_320 / escala
    alvo = metros_por_pt * largura * 0.25
    passo = 10 ** math.floor(math.log10(alvo))
    for m in (5, 2, 1):
        if m * passo <= alvo:
            passo *= m
            break
    comp = passo / metros_por_pt
    rotulo = f"{passo / 1000:g} km" if passo >= 1000 else f"{passo:g} m"
    d.add(Line(8, 8, 8 + comp, 8, strokeColor=colors.black, strokeWidth=1.2))
    for x in (8, 8 + comp):
        d.add(Line(x, 6, x, 10, strokeColor=colors.black, strokeWidth=1))
    d.add(String(8 + comp + 4, 5.5, rotulo, fontName="Helvetica", fontSize=7))
    # Norte
    nx, ny = largura - 12, altura - 22
    d.add(Polygon([nx, ny + 12, nx - 4, ny, nx + 4, ny], fillColor=colors.black, strokeColor=None))
    d.add(String(nx, ny + 14, "N", fontName="Helvetica-Bold", fontSize=7, textAnchor="middle"))
    return d


def _legenda() -> Table:
    itens = [("Área analisada", colors.HexColor("#e6f2ec"), VERDE),
             ("Não conforme", COR_CONFLITO["bad"], COR_CONFLITO["bad"]),
             ("Atenção", COR_CONFLITO["warn"], COR_CONFLITO["warn"]),
             ("Informativo", COR_CONFLITO["info"], COR_CONFLITO["info"])]
    celulas = []
    for nome, fundo, borda in itens:
        amostra = Drawing(8, 8)
        amostra.add(Rect(0, 0, 8, 8, fillColor=fundo, strokeColor=borda, strokeWidth=0.8))
        celulas += [amostra, Paragraph(nome, E_PEQ)]
    t = Table([celulas[:4], celulas[4:]], colWidths=[12, 30 * mm, 12, 30 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 1),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
    return t


class _ImagemSatelite(Flowable):
    """Recorte Sentinel-2 com o contorno da área analisada e das sobreposições desenhados por cima."""

    def __init__(self, png: bytes, meta: dict, resultado: dict, largura: float, altura_max: float):
        super().__init__()
        self.png, self.meta, self.resultado = png, meta, resultado
        proporcao = meta["altura_px"] / meta["largura_px"]
        self.width = min(largura, altura_max / proporcao)
        self.height = self.width * proporcao

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        x0, y0, x1, y1 = self.meta["bbox"]

        def xy(p):
            return (p[0] - x0) / (x1 - x0) * w, (p[1] - y0) / (y1 - y0) * h

        def contorno(geom, cor, espessura, tracejado=None, preencher=False):
            c.setStrokeColor(cor)
            c.setLineWidth(espessura)
            c.setDash(*(tracejado or ([], 0)))
            if preencher:
                c.setFillColor(cor, alpha=0.38)
            for pol in _aneis(geom):
                caminho = c.beginPath()
                for anel in pol:  # buracos no mesmo caminho, com regra par-ímpar
                    if len(anel) < 3:
                        continue
                    caminho.moveTo(*xy(anel[0]))
                    for p in anel[1:]:
                        caminho.lineTo(*xy(p))
                    caminho.close()
                c.drawPath(caminho, stroke=1, fill=1 if preencher else 0, fillMode=0)

        c.saveState()
        c.drawImage(ImageReader(BytesIO(self.png)), 0, 0, w, h)
        caminho = c.beginPath()
        caminho.rect(0, 0, w, h)
        c.clipPath(caminho, stroke=0, fill=0)
        # Na imagem "depois", o que é Não conforme fica pintado: é onde se vê a mudança
        depois = self.meta.get("tipo") == "depois"
        for f in self.resultado.get("conflitos", {}).get("features", []):
            st = f["properties"].get("status")
            if st == "bad" and depois:
                contorno(f["geometry"], COR_CONFLITO["bad"], 0.9, preencher=True)
            elif st in ("bad", "warn"):
                contorno(f["geometry"], COR_CONFLITO[st], 0.9, ([2, 1.5], 0))
        contorno(self.resultado["area"]["geometria"], colors.HexColor("#ffe14d"), 1.5)
        c.restoreState()
        c.setStrokeColor(BORDA)
        c.setLineWidth(0.6)
        c.rect(0, 0, w, h, stroke=1, fill=0)


def _secao_imagens(resultado: dict, imagens: dict[str, bytes], largura_util: float) -> list:
    metas = resultado.get("imagens") or []
    if not metas:
        return []
    coluna = (largura_util - 6 * mm) / 2
    anos = sorted({m["data"][:4] for m in metas if m.get("data")}) or [str(datetime.now().year)]
    celulas = []
    for meta in metas:
        png = imagens.get(meta["tipo"])
        # O PNG só entra no laudo se for exatamente o que foi registrado na análise
        if png and meta.get("sha256") == hashlib.sha256(png).hexdigest():
            data = datetime.fromisoformat(meta["data"]).strftime("%d/%m/%Y")
            legenda = (f"<b>{_esc(meta['titulo'])}</b><br/>Imagem de {data} · {_esc(meta.get('satelite') or 'Sentinel-2')}"
                       f" · nuvens na cena: {_num(meta.get('nuvens_pct'), 1)}%")
            celulas.append([_ImagemSatelite(png, meta, resultado, coluna, 95 * mm), Spacer(1, 2),
                            Paragraph(legenda, E_PEQ)])
        else:
            celulas.append([Paragraph(f"<b>{_esc(meta['titulo'])}</b><br/>"
                                      f"{_esc(meta.get('indisponivel') or 'Imagem indisponível.')}", E_PEQ)])
    t = Table([celulas], colWidths=[coluna + 3 * mm] * len(celulas), hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm)]))
    nota = Paragraph(
        "Cor natural, resolução de 10 m. Contorno amarelo: área analisada; em vermelho na imagem mais recente: áreas "
        "Não conformes; tracejado: demais sobreposições com ocorrência. Compare as duas imagens para ver mudanças de uso do solo desde 2020. "
        f"{_esc(metas[0].get('atribuicao') or '')} ({', '.join(anos)}).", E_AVISO)
    return [Paragraph("Imagens de satélite: antes e depois", E_H2), KeepTogether([t, Spacer(1, 3), nota])]


def _qr(url: str, lado: float) -> Drawing:
    w = QrCodeWidget(url, barLevel="M")
    x1, y1, x2, y2 = w.getBounds()
    d = Drawing(lado, lado, transform=[lado / (x2 - x1), 0, 0, lado / (y2 - y1), 0, 0])
    d.add(w)
    return d


# ---------- documento ----------
def gerar_pdf(resultado: dict, url_verificacao: str, imagens: dict[str, bytes] | None = None) -> bytes:
    protocolo = resultado["protocolo"]
    integridade = resultado.get("integridade") or codigo_integridade(resultado)
    area = resultado["area"]
    im = area.get("imovel") or {}
    geral = resultado["geral"]
    largura_util = A4[0] - 36 * mm

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title=f"Laudo GeoLEM {protocolo}", author="GeoLEM",
                            subject="Laudo de conformidade socioambiental")

    def rodape(canvas, doc_):
        canvas.saveState()
        canvas.setStrokeColor(BORDA)
        canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(CINZA)
        canvas.drawString(18 * mm, 10 * mm, f"GeoLEM · Protocolo {protocolo} · Verifique em {url_verificacao}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Página {doc_.page}")
        canvas.restoreState()

    h = []
    # Cabeçalho
    topo = Table([[
        [Paragraph('<font color="#1d7a52"><b>GeoLEM</b></font>', _estilo("marca", fontSize=11, leading=13)),
         Paragraph("Laudo de Conformidade Socioambiental", E_TITULO),
         Paragraph("Cruzamento automatizado da área com bases públicas oficiais", E_SUB)],
        [Paragraph(f"Protocolo<br/><b>{_esc(protocolo)}</b>", E_DIR),
         Paragraph(f"Emitido em {_data_hora(resultado.get('criado_em'))} (horário de Brasília)", E_DIR)],
    ]], colWidths=[largura_util * 0.62, largura_util * 0.38])
    topo.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1.5, VERDE),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    h += [topo, Spacer(1, 8)]

    # Resultado geral
    cor_txt, cor_fundo = CORES[geral]
    ocorrencias = sum(1 for c in resultado["checks"] if c["status"] in ("warn", "bad"))
    faixa = Table([[Paragraph(
        f'<font color="#{cor_txt.hexval()[2:]}" size="13"><b>{_esc(resultado["rotulo_geral"])}</b></font><br/>'
        f'{_esc(resultado["resumo"])} {ocorrencias} verificação(ões) com ocorrência.', _estilo("faixa", leading=15))]],
        colWidths=[largura_util])
    faixa.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), cor_fundo), ("BOX", (0, 0), (-1, -1), 0.8, cor_txt),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    h += [faixa, Spacer(1, 8)]

    # Identificação + mapa
    if im:
        origem = {"SIGEF": "SIGEF (INCRA)", "CAR": "CAR (SICAR)"}.get(im.get("tipo"), im.get("tipo") or "-")
    else:
        origem = "Polígono informado pelo usuário"
    municipio = f"{im['municipio']}/{im['uf']}" if im.get("municipio") and im.get("uf") else im.get("municipio")
    lon, lat = area.get("ponto_interno") or (None, None)
    dados = [
        ("Área", area.get("nome")), ("Origem", origem), ("Código", im.get("codigo")), ("Município", municipio),
        ("Situação", im.get("situacao")), ("Área total", f"{_num(area.get('area_ha'))} ha"),
        ("Ponto interno", f"{lat:.5f}, {lon:.5f}" if lat is not None else None),
    ]
    tab_id = Table([[Paragraph(k, E_CEL_B), Paragraph(_esc(v), E_CEL)] for k, v in dados if v],
                   colWidths=[24 * mm, largura_util * 0.48 - 24 * mm])
    tab_id.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDA), ("BACKGROUND", (0, 0), (0, -1), FUNDO),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    lado_mapa = largura_util * 0.5
    mapa = [_mapa(resultado, lado_mapa, lado_mapa * 0.78), Spacer(1, 2), _legenda()]
    bloco = Table([[tab_id, mapa]], colWidths=[largura_util * 0.5, largura_util * 0.5])
    bloco.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (0, 0), 8), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    h += [Paragraph("Área analisada", E_H2), bloco]
    h += _secao_imagens(resultado, imagens or {}, largura_util)

    # Verificações
    linhas = [[Paragraph(t, E_CEL_B) for t in ("Verificação", "Resultado", "Área (ha)", "Detalhes")]]
    estilos = [("GRID", (0, 0), (-1, -1), 0.5, BORDA), ("BACKGROUND", (0, 0), (-1, 0), FUNDO),
               ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for i, c in enumerate(resultado["checks"], start=1):
        detalhes = "<br/>".join(
            _esc(it["descricao"]) + (f": {_num(it['ha'])} ha" if it.get("ha") is not None else "") for it in c["itens"])
        linhas.append([
            Paragraph(f"<b>{_esc(c['titulo'])}</b><br/><font color='#5b6660' size='7'>{_esc(c['base'])}</font>", E_CEL),
            _selo(c["status"]),
            Paragraph(_num(c["ha"]) if c["ha"] else "-", _estilo("num", fontSize=8, leading=10, alignment=TA_RIGHT)),
            Paragraph(detalhes, E_CEL),
        ])
        if c["status"] in ("warn", "bad"):
            estilos.append(("BACKGROUND", (1, i), (1, i), CORES[c["status"]][1]))
    tab = Table(linhas, colWidths=[52 * mm, 22 * mm, 18 * mm, largura_util - 92 * mm], repeatRows=1)
    tab.setStyle(TableStyle(estilos))
    h += [Paragraph("Verificações", E_H2), tab]

    # Bases consultadas
    linhas = [[Paragraph(t, E_CEL_B) for t in ("Base consultada", "Atualizada em", "Registros na região")]]
    for b in resultado.get("bases", []):
        linhas.append([Paragraph(f"{_esc(b['descricao'])}<br/><font color='#5b6660' size='6.5'>{_esc(b.get('url'))}</font>",
                                 E_CEL),
                       Paragraph(_data_hora(b.get("atualizado_em")), E_CEL),
                       Paragraph(_num(b.get("registros"), 0), _estilo("num2", fontSize=8, leading=10, alignment=TA_RIGHT))])
    tab_b = Table(linhas, colWidths=[largura_util - 62 * mm, 32 * mm, 30 * mm], repeatRows=1)
    tab_b.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, BORDA), ("BACKGROUND", (0, 0), (-1, 0), FUNDO),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    h += [Paragraph("Bases consultadas", E_H2), tab_b]

    # Metodologia
    area_min = _num(resultado.get("parametros", {}).get("area_minima_ha"), 1)
    h += [Paragraph("Critérios e limitações", E_H2), Paragraph(
        "Desmatamento posterior a 31/12/2020 (data de corte do Regulamento UE 2023/1115, EUDR) é classificado como "
        "<b>Não conforme</b>; entre 22/07/2008 e 31/12/2020 (marco do Código Florestal, Lei 12.651/2012), como "
        "<b>Atenção</b>. A classificação usa a data da imagem de satélite do PRODES quando disponível, pois o ano PRODES vai "
        "de agosto a julho. Embargos do IBAMA, UCs de Proteção Integral, Terras Indígenas e territórios quilombolas "
        "reconhecidos (portaria, decreto ou título) resultam em <b>Não conforme</b>; UCs de Uso Sustentável, territórios "
        "quilombolas em estudo e assentamentos da reforma agrária, em <b>Atenção</b>. A Reserva Legal mínima considerada é de 20% (Cerrado fora da Amazônia "
        f"Legal). Sobreposições menores que {area_min} ha são desconsideradas por serem, em geral, ruído de borda entre "
        "bases. Áreas calculadas sobre o elipsoide (geodésicas).", E_PEQ),
        Spacer(1, 4),
        Paragraph("Este laudo é um resultado automatizado, válido para as bases e datas indicadas. Não substitui vistoria "
                  "de campo nem parecer de profissional habilitado, e não atesta a regularidade dominial do imóvel.",
                  E_AVISO)]

    # Autenticidade
    qr = _qr(url_verificacao, 30 * mm)
    texto = Paragraph(
        "<b>Autenticidade</b><br/>Aponte a câmera para o QR code ou acesse o endereço abaixo para conferir este laudo "
        f"na base do GeoLEM.<br/><font color='#1d7a52'>{_esc(url_verificacao)}</font><br/><br/>"
        f"<b>Código de integridade (SHA-256)</b><br/><font name='Courier' size='7'>{integridade[:32]}<br/>"
        f"{integridade[32:]}</font>", E_PEQ)
    aut = Table([[qr, texto]], colWidths=[34 * mm, largura_util - 34 * mm])
    aut.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOX", (0, 0), (-1, -1), 0.6, BORDA),
                             ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafcfb")),
                             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    h += [Spacer(1, 10), KeepTogether([aut])]

    doc.build(h, onFirstPage=rodape, onLaterPages=rodape)
    return buf.getvalue()
