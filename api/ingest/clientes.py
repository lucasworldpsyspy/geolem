"""Clientes paginados para os serviços públicos (GeoServer WFS e ArcGIS FeatureServer)."""
import time
from collections.abc import Iterator

import httpx

CABECALHOS = {"User-Agent": "GeoLEM/0.2 (ingestao de dados publicos)"}
PAGINA = 1000


def _get(cliente: httpx.Client, url: str, params: dict, tentativas: int = 4) -> dict:
    for n in range(1, tentativas + 1):
        try:
            r = cliente.get(url, params=params, timeout=180)
            r.raise_for_status()
            dados = r.json()
            if isinstance(dados, dict) and "error" in dados:
                raise RuntimeError(f"Serviço retornou erro: {dados['error']}")
            return dados
        except (httpx.HTTPError, ValueError, RuntimeError) as e:
            if n == tentativas:
                raise
            espera = 5 * n
            print(f"    falha ({e}); nova tentativa em {espera}s")
            time.sleep(espera)
    raise AssertionError("inalcançável")


def wfs(url: str, tipo: str, bbox: tuple, ordenar_por: str) -> Iterator[dict]:
    """GeoServer WFS 2.0 em GeoJSON. bbox = (lon_min, lat_min, lon_max, lat_max)."""
    lon_min, lat_min, lon_max, lat_max = bbox
    inicio = 0
    with httpx.Client(headers=CABECALHOS) as cliente:
        while True:
            fc = _get(cliente, url, {
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": tipo, "outputFormat": "application/json", "srsName": "EPSG:4326",
                # Em WFS 2.0 com URN, a ordem dos eixos de EPSG:4326 é latitude, longitude
                "bbox": f"{lat_min},{lon_min},{lat_max},{lon_max},urn:ogc:def:crs:EPSG::4326",
                "count": PAGINA, "startIndex": inicio, "sortBy": ordenar_por,
            })
            feicoes = fc.get("features") or []
            yield from feicoes
            print(f"    {inicio + len(feicoes)} feições lidas")
            if len(feicoes) < PAGINA:
                return
            inicio += PAGINA


def arcgis(url_camada: str, bbox: tuple, campos: list[str], ordenar_por: str = "objectid") -> Iterator[dict]:
    """ArcGIS FeatureServer /query em GeoJSON."""
    lon_min, lat_min, lon_max, lat_max = bbox
    deslocamento = 0
    with httpx.Client(headers=CABECALHOS) as cliente:
        while True:
            fc = _get(cliente, f"{url_camada}/query", {
                "where": "1=1",
                "geometry": f"{lon_min},{lat_min},{lon_max},{lat_max}",
                "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": ",".join(campos), "returnGeometry": "true", "outSR": 4326,
                "orderByFields": ordenar_por,
                "resultOffset": deslocamento, "resultRecordCount": PAGINA, "f": "geojson",
            })
            feicoes = fc.get("features") or []
            yield from feicoes
            print(f"    {deslocamento + len(feicoes)} feições lidas")
            excedeu = fc.get("exceededTransferLimit") or (fc.get("properties") or {}).get("exceededTransferLimit")
            if not feicoes or (len(feicoes) < PAGINA and not excedeu):
                return
            deslocamento += len(feicoes)
