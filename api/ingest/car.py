"""Importa imóveis do CAR a partir do shapefile baixado no SICAR.

O SICAR não oferece serviço aberto para a Bahia. Baixe manualmente em
https://consultapublica.car.gov.br/publico/estados/downloads (escolha BA e o município),
salve os ZIPs na pasta geolem/dados e rode, dentro do container:

    python -m ingest.car dados/AREA_IMOVEL.zip --reserva-legal dados/RESERVA_LEGAL.zip --fonte car_ba_lem
"""
import argparse
import json
import sys

import geopandas as gpd

from app.config import DATABASE_URL

from .carregar import carregar, conectar
from .fontes import Fonte, texto


def _ler_gdf(caminho: str) -> gpd.GeoDataFrame:
    origem = f"zip://{caminho}" if caminho.lower().endswith(".zip") else caminho
    gdf = gpd.read_file(origem)
    if gdf.crs is None:
        gdf = gdf.set_crs(4674)  # SIRGAS 2000, padrão do SICAR
    gdf = gdf.to_crs(4326)
    gdf.columns = [c.lower() if c != "geometry" else c for c in gdf.columns]
    return gdf


def _campo(p: dict, *nomes):
    for n in nomes:
        if p.get(n) not in (None, ""):
            return p[n]
    return None


def mapear_car(p: dict) -> dict:
    return {
        "tipo": "CAR",
        "codigo": texto(_campo(p, "cod_imovel", "codigo")),
        "nome": texto(_campo(p, "nom_imovel", "nome")),
        "municipio": texto(_campo(p, "municipio", "nom_munici", "nom_municipio")),
        "uf": texto(_campo(p, "cod_estado", "uf")) or "BA",
        "situacao": texto(_campo(p, "ind_status", "des_condic", "situacao")),
    }


def atualizar_reserva_legal(conn, fonte_id: str, caminho_rl: str) -> int:
    rl = _ler_gdf(caminho_rl)
    rl = rl[rl.geometry.notna()]
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_rl (cod_imovel text, geom geometry(MultiPolygon, 4326))")
    conn.execute("TRUNCATE tmp_rl")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tmp_rl VALUES (%s, geo.normalizar(ST_GeomFromGeoJSON(%s)))",
            [(texto(r.get("cod_imovel")), json.dumps(r.geometry.__geo_interface__)) for _, r in rl.iterrows()],
        )
    if "cod_imovel" in rl.columns:
        # Soma a RL declarada por código do imóvel (dissolve antes para não contar sobreposição duas vezes)
        cur = conn.execute(
            """UPDATE geo.imoveis i SET reserva_legal_ha = s.ha
               FROM (SELECT cod_imovel, ST_Area(ST_Union(geom)::geography) / 10000.0 AS ha
                     FROM tmp_rl GROUP BY cod_imovel) s
               WHERE i.fonte = %s AND i.codigo = s.cod_imovel""",
            (fonte_id,),
        )
    else:
        cur = conn.execute(
            """UPDATE geo.imoveis i SET reserva_legal_ha = s.ha
               FROM (SELECT i2.id, ST_Area(ST_Union(ST_Intersection(i2.geom, r.geom))::geography) / 10000.0 AS ha
                     FROM geo.imoveis i2 JOIN tmp_rl r ON ST_Intersects(i2.geom, r.geom)
                     WHERE i2.fonte = %s GROUP BY i2.id) s
               WHERE i.id = s.id""",
            (fonte_id,),
        )
    return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.car")
    ap.add_argument("area_imovel", help="ZIP ou SHP da camada AREA_IMOVEL do SICAR")
    ap.add_argument("--reserva-legal", help="ZIP ou SHP da camada RESERVA_LEGAL do SICAR (opcional)")
    ap.add_argument("--fonte", default="car_ba", help="identificador desta carga (ex.: car_ba_lem)")
    args = ap.parse_args()

    gdf = _ler_gdf(args.area_imovel)
    print(f"{len(gdf)} imóveis lidos de {args.area_imovel}")

    def ler(_bbox):
        for _, r in gdf.iterrows():
            if r.geometry is None:
                continue
            props = {k: (None if v != v else v) for k, v in r.drop(labels="geometry").items()}  # NaN -> None
            yield {"type": "Feature", "properties": props, "geometry": r.geometry.__geo_interface__}

    fonte = Fonte(args.fonte, f"CAR: arquivo {args.area_imovel.split('/')[-1]} (SICAR)", "geo.imoveis",
                  "https://consultapublica.car.gov.br", ler, mapear_car)
    with conectar(DATABASE_URL) as conn:
        carregar(conn, fonte, ())
        if args.reserva_legal:
            n = atualizar_reserva_legal(conn, args.fonte, args.reserva_legal)
            print(f"Reserva legal calculada para {n} imóveis.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
