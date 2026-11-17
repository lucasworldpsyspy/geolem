"""Vector tiles (MVT) gerados direto do PostGIS."""

# camada -> (tabela, colunas exportadas, zoom mínimo). Lista fechada: nada vem do usuário.
CAMADAS = {
    "desmatamento": ("geo.desmatamento", "t.id, t.ano, to_char(t.data_imagem, 'DD/MM/YYYY') AS data_imagem, t.fonte", 8),
    "embargos": ("geo.embargos", "t.id, t.numero_tad, to_char(t.data_embargo, 'DD/MM/YYYY') AS data_embargo, "
                 "left(t.infracao, 200) AS infracao, t.area_embargada_ha", 5),
    "ucs": ("geo.unidades_conservacao", "t.id, t.nome, t.categoria, t.grupo, t.esfera", 4),
    "tis": ("geo.terras_indigenas", "t.id, t.nome, t.etnia, t.fase", 4),
    "imoveis": ("geo.imoveis", "t.id, t.tipo, t.codigo, t.nome, round(t.area_ha::numeric, 1) AS area_ha", 9),
}


def gerar_tile(conn, camada: str, z: int, x: int, y: int) -> bytes:
    tabela, colunas, zmin = CAMADAS[camada]
    if z < zmin:
        return b""
    with conn.cursor() as cur:
        cur.execute(
            f"""WITH b AS (SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS env),
                     b4326 AS (SELECT ST_Transform(env, 4326) AS env FROM b),
                mvt AS (
                  SELECT {colunas},
                         ST_AsMVTGeom(ST_Transform(t.geom, 3857), b.env, 4096, 64, true) AS geom
                  FROM {tabela} t, b, b4326
                  WHERE t.geom && b4326.env
                )
                SELECT ST_AsMVT(mvt.*, %(nome)s, 4096, 'geom') AS tile FROM mvt WHERE geom IS NOT NULL""",
            {"z": z, "x": x, "y": y, "nome": camada},
        )
        linha = cur.fetchone()
    return bytes(linha["tile"]) if linha and linha["tile"] else b""
