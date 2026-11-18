"""Leitura de arquivos geográficos enviados pelo usuário (KML, KMZ, Shapefile em ZIP, GeoJSON, GPKG)."""
import json
import os
import tempfile
import zipfile

import geopandas as gpd
from shapely.geometry import mapping
from shapely.ops import unary_union

EXTENSOES = {".kml", ".kmz", ".zip", ".geojson", ".json", ".gpkg"}


class ErroArquivo(ValueError):
    pass


def _ler(caminho: str, ext: str) -> gpd.GeoDataFrame:
    if ext == ".zip":
        with zipfile.ZipFile(caminho) as z:
            shps = [n for n in z.namelist() if n.lower().endswith(".shp")]
        if not shps:
            raise ErroArquivo("O ZIP não contém nenhum arquivo .shp.")
        return gpd.read_file(f"zip://{caminho}!{shps[0]}")
    if ext == ".kmz":
        with zipfile.ZipFile(caminho) as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ErroArquivo("O KMZ não contém nenhum arquivo .kml.")
            destino = os.path.join(os.path.dirname(caminho), "doc.kml")
            with open(destino, "wb") as f:
                f.write(z.read(kmls[0]))
        return gpd.read_file(destino)
    return gpd.read_file(caminho)


def _poligonos(nome_arquivo: str, conteudo: bytes) -> gpd.GeoDataFrame:
    ext = os.path.splitext(nome_arquivo.lower())[1]
    if ext not in EXTENSOES:
        raise ErroArquivo("Formato não suportado. Envie KML, KMZ, Shapefile (.zip), GeoJSON ou GPKG.")
    with tempfile.TemporaryDirectory() as tmp:
        caminho = os.path.join(tmp, "entrada" + ext)
        with open(caminho, "wb") as f:
            f.write(conteudo)
        try:
            gdf = _ler(caminho, ext)
        except ErroArquivo:
            raise
        except Exception as e:
            raise ErroArquivo(f"Não foi possível ler o arquivo: {e}") from e

    if gdf.empty:
        raise ErroArquivo("O arquivo não tem feições.")
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)  # GeoJSON/KML sem CRS são WGS84 por definição
    gdf = gdf.to_crs(4326)
    poligonos = gdf[gdf.geometry.notna() & gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
    if poligonos.empty:
        raise ErroArquivo("Nenhum polígono encontrado no arquivo (pontos e linhas não definem área).")
    return poligonos


def _texto(linha, *chaves) -> str | None:
    for c in chaves:
        for col in linha.index:
            if str(col).strip().lower() == c.lower():
                v = linha[col]
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, (int, float)) and v == v:  # descarta NaN
                    return str(int(v)) if float(v).is_integer() else str(v)
    return None


def ler_geometria(nome_arquivo: str, conteudo: bytes) -> dict:
    """Todos os polígonos do arquivo unidos numa única área."""
    poligonos = _poligonos(nome_arquivo, conteudo)
    geom = unary_union([g.buffer(0) if not g.is_valid else g for g in poligonos.geometry])
    props = {}
    primeira = poligonos.iloc[0]
    for chave in ("nome", "name", "Name", "NOME", "cod_imovel", "COD_IMOVEL"):
        if chave in poligonos.columns and isinstance(primeira[chave], str) and primeira[chave].strip():
            props.setdefault("nome", primeira[chave].strip())
    return {
        "type": "Feature",
        "properties": {**props, "feicoes": int(len(poligonos)), "arquivo": nome_arquivo},
        "geometry": json.loads(json.dumps(mapping(geom))),
    }


def ler_feicoes(nome_arquivo: str, conteudo: bytes) -> list[dict]:
    """Cada polígono do arquivo como uma área separada (carteira em lote), com nome e fornecedor dos atributos."""
    saida = []
    for i, (_, linha) in enumerate(_poligonos(nome_arquivo, conteudo).iterrows(), start=1):
        g = linha.geometry if linha.geometry.is_valid else linha.geometry.buffer(0)
        saida.append({
            "linha": i,
            "nome": _texto(linha, "nome", "name", "fazenda", "imovel", "nom_imovel", "cod_imovel") or f"Área {i}",
            "fornecedor": _texto(linha, "fornecedor", "produtor", "proprietario"),
            "codigo_externo": _texto(linha, "codigo_externo", "codigo_erp", "cod_fornecedor", "codigo"),
            "cultura": _texto(linha, "cultura", "commodity", "produto"),
            "geometria": json.loads(json.dumps(mapping(g))),
        })
    return saida
