"""Exportação da geolocalização da área no formato GeoJSON aceito pelo sistema da UE (TRACES) para a declaração
de diligência do Regulamento (UE) 2023/1115 (EUDR).

Regras do formato: FeatureCollection em WGS84 (EPSG:4326), coordenadas em graus decimais com até 6 casas,
um Feature por parcela de produção, com Polygon/MultiPolygon (obrigatório acima de 4 ha) ou Point.
Propriedades usadas: ProductionPlace (nome da parcela), ProducerCountry (ISO 3166-1 alfa-2) e Area (ha).
"""


def _arredondar(coords):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], 6), round(coords[1], 6)]
    return [_arredondar(c) for c in coords]


def geojson_eudr(resultado: dict) -> dict:
    area = resultado["area"]
    geom = area["geometria"]
    if geom["type"] == "MultiPolygon" and len(geom["coordinates"]) == 1:
        geom = {"type": "Polygon", "coordinates": geom["coordinates"][0]}
    geom = {"type": geom["type"], "coordinates": _arredondar(geom["coordinates"])}

    im = area.get("imovel") or {}
    lugar = area.get("nome") or "Área sem nome"
    if im.get("codigo"):
        lugar = f"{lugar} ({im.get('tipo') or 'código'} {im['codigo']})"
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "ProductionPlace": lugar[:250],
                "ProducerCountry": "BR",
                "Area": round(area["area_ha"], 4),
            },
        }],
    }
