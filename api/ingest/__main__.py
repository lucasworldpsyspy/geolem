"""Carga das bases públicas.

Exemplos (dentro do container da API):
    python -m ingest --listar
    python -m ingest todas
    python -m ingest prodes_cerrado embargos_ibama
    python -m ingest --bbox=-46.7,-12.9,-45.0,-11.3 todas
"""
import argparse
import sys

from app.config import DATABASE_URL, REGIAO_BBOX

from .carregar import carregar, conectar
from .fontes import FONTES, mapbiomas_configurado


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest", description="Carrega bases públicas no PostGIS do GeoLEM.")
    ap.add_argument("fontes", nargs="*", help="ids das fontes ou 'todas'")
    ap.add_argument("--listar", action="store_true", help="lista as fontes disponíveis")
    ap.add_argument("--bbox", help="lon_min,lat_min,lon_max,lat_max (padrão: REGIAO_BBOX)")
    args = ap.parse_args()

    if args.listar or not args.fontes:
        for f in FONTES.values():
            print(f"  {f.id:<22} {f.descricao}")
        return 0

    ids = list(FONTES) if args.fontes == ["todas"] else args.fontes
    if args.fontes == ["todas"] and not mapbiomas_configurado():
        ids.remove("mapbiomas_alerta")
        print("MapBiomas Alerta ignorado (sem MAPBIOMAS_EMAIL/MAPBIOMAS_SENHA no .env).")
    desconhecidas = [i for i in ids if i not in FONTES]
    if desconhecidas:
        print(f"Fontes desconhecidas: {', '.join(desconhecidas)}. Use --listar.")
        return 2

    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else REGIAO_BBOX
    print(f"Região: {bbox}")
    falhas = []
    with conectar(DATABASE_URL) as conn:
        for i in ids:
            print(f"→ {FONTES[i].descricao}")
            try:
                carregar(conn, FONTES[i], bbox)
            except Exception:  # segue para as próximas fontes
                falhas.append(i)
    if falhas:
        print(f"\nConcluído com falhas em: {', '.join(falhas)}. Rode de novo só essas fontes.")
        return 1
    print("\nTodas as fontes carregadas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
