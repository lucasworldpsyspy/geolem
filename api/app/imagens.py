"""Imagens de satélite Sentinel-2 de antes e depois da área, para o laudo.

"Antes" é a melhor cena do período seco de 2020, anterior à data de corte do EUDR (31/12/2020).
"Depois" é a melhor cena do período seco mais recente. No oeste da Bahia, de junho a setembro quase não há nuvem.

As cenas vêm do catálogo público do Microsoft Planetary Computer (Copernicus Sentinel-2 L2A, licença que permite
uso comercial com atribuição). Cada imagem é um recorte RGB (cor natural) da área com margem, montado a partir de
cenas da mesma passagem do satélite, para que a imagem tenha uma única data.
"""
import hashlib
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO

import httpx
from PIL import Image

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
RECORTE = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
COLECAO = "sentinel-2-l2a"
ATRIBUICAO = "Contém dados modificados do Copernicus Sentinel-2 (ESA), via Microsoft Planetary Computer"

LARGURA_PX = 900
ALTURA_MAX_PX = 900
NUVENS_MAX = 10          # % da cena
COBERTURA_MIN = 0.97     # fração do recorte que precisa ter imagem
TENTATIVAS = 3           # passagens testadas por período
TEMPO_LIMITE = 40        # s por requisição
CABECALHOS = {"User-Agent": "GeoLEM/0.3"}


def ultimo_periodo_seco(hoje: date) -> int:
    """Ano do período seco (jun-set) mais recente já encerrado."""
    return hoje.year if hoje.month >= 10 else hoje.year - 1


def janela(bbox: tuple[float, float, float, float], margem: float = 0.15,
           lado_min_graus: float = 0.006) -> tuple[float, float, float, float]:
    """Recorte com margem ao redor da área, tamanho mínimo (~650 m) e proporção entre 3:4 e 4:3, para que imóveis
    compridos também mostrem o entorno e caibam lado a lado no laudo."""
    x0, y0, x1, y1 = bbox
    kx = math.cos(math.radians((y0 + y1) / 2))
    dx, dy = max(x1 - x0, lado_min_graus), max(y1 - y0, lado_min_graus)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = dx * (1 + 2 * margem), dy * (1 + 2 * margem)
    largura_real, altura_real = dx * kx, dy
    if altura_real > largura_real * 4 / 3:
        dx = altura_real * 3 / 4 / kx
    elif largura_real > altura_real * 4 / 3:
        dy = largura_real * 3 / 4
    return (round(cx - dx / 2, 6), round(cy - dy / 2, 6), round(cx + dx / 2, 6), round(cy + dy / 2, 6))


def tamanho_px(bbox: tuple[float, float, float, float]) -> tuple[int, int]:
    """Pixels proporcionais às distâncias reais (o grau de longitude encolhe com o cosseno da latitude)."""
    x0, y0, x1, y1 = bbox
    kx = math.cos(math.radians((y0 + y1) / 2))
    proporcao = (y1 - y0) / max((x1 - x0) * kx, 1e-9)
    largura = LARGURA_PX
    altura = round(largura * proporcao)
    if altura > ALTURA_MAX_PX:
        largura, altura = round(ALTURA_MAX_PX / proporcao), ALTURA_MAX_PX
    return max(largura, 64), max(altura, 64)


def _passagens(cliente: httpx.Client, bbox, inicio: str, fim: str) -> list[list[dict]]:
    """Cenas do período agrupadas por passagem (mesma data e satélite), das menos nubladas para as mais."""
    r = cliente.post(STAC, json={
        "collections": [COLECAO], "bbox": list(bbox), "datetime": f"{inicio}T00:00:00Z/{fim}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": NUVENS_MAX}},
        "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}], "limit": 60,
    }, timeout=TEMPO_LIMITE)
    r.raise_for_status()
    grupos: dict[tuple, list[dict]] = defaultdict(list)
    for f in r.json().get("features", []):
        p = f["properties"]
        grupos[(p["datetime"][:10], p.get("platform"))].append(f)
    return sorted(grupos.values(), key=lambda g: max(f["properties"]["eo:cloud_cover"] for f in g))


def _recortar(cliente: httpx.Client, item_id: str, bbox, w: int, h: int) -> Image.Image:
    x0, y0, x1, y1 = bbox
    r = cliente.get(f"{RECORTE}/{x0},{y0},{x1},{y1}/{w}x{h}.png",
                    params={"collection": COLECAO, "item": item_id, "assets": "visual",
                            "asset_bidx": "visual|1,2,3", "nodata": 0}, timeout=TEMPO_LIMITE)
    r.raise_for_status()
    return Image.open(BytesIO(r.content)).convert("RGBA")


def _cobertura(img: Image.Image) -> float:
    alfa = img.getchannel("A")
    return sum(1 for v in alfa.getdata() if v) / (img.width * img.height)


def imagem_do_periodo(tipo: str, titulo: str, bbox, inicio: str, fim: str) -> tuple[dict, bytes | None]:
    """Devolve (metadados, png). Sem cena adequada, png é None e os metadados explicam o motivo."""
    w, h = tamanho_px(bbox)
    meta = {"tipo": tipo, "titulo": titulo, "periodo": [inicio, fim], "bbox": list(bbox), "largura_px": w,
            "altura_px": h, "atribuicao": ATRIBUICAO}
    with httpx.Client(headers=CABECALHOS) as cliente:
        for passagem in _passagens(cliente, bbox, inicio, fim)[:TENTATIVAS]:
            mosaico = Image.new("RGBA", (w, h))
            for item in passagem:
                mosaico.alpha_composite(_recortar(cliente, item["id"], bbox, w, h))
                if _cobertura(mosaico) >= 0.999:
                    break
            if _cobertura(mosaico) >= COBERTURA_MIN:
                buf = BytesIO()
                mosaico.convert("RGB").save(buf, "PNG", optimize=True)
                png = buf.getvalue()
                p = passagem[0]["properties"]
                meta.update({
                    "data": p["datetime"][:10], "satelite": p.get("platform"),
                    "cenas": [i["id"] for i in passagem],
                    "nuvens_pct": round(max(i["properties"]["eo:cloud_cover"] for i in passagem), 2),
                    "sha256": hashlib.sha256(png).hexdigest(),
                })
                return meta, png
    meta["indisponivel"] = "Nenhuma cena sem nuvens cobrindo a área no período."
    return meta, None


def imagens_antes_depois(bbox_area: tuple[float, float, float, float], hoje: date) -> list[tuple[dict, bytes | None]]:
    """As duas imagens, buscadas em paralelo. Falha de rede vira 'indisponivel', nunca derruba a análise."""
    b = janela(bbox_area)
    ano = ultimo_periodo_seco(hoje)
    pedidos = [
        ("antes", "Antes: período seco de 2020 (anterior ao corte do EUDR)", "2020-06-01", "2020-09-30"),
        ("depois", f"Depois: período seco de {ano} (mais recente)", f"{ano}-06-01", f"{ano}-09-30"),
    ]

    def buscar(pedido):
        tipo, titulo, inicio, fim = pedido
        try:
            return imagem_do_periodo(tipo, titulo, b, inicio, fim)
        except Exception as e:  # serviço fora do ar, tempo esgotado etc.
            return ({"tipo": tipo, "titulo": titulo, "periodo": [inicio, fim], "bbox": list(b),
                     "indisponivel": f"Serviço de imagens indisponível no momento ({type(e).__name__})."}, None)

    with ThreadPoolExecutor(max_workers=2) as ex:
        return list(ex.map(buscar, pedidos))
