"""Leitura de imóveis cadastrados (SIGEF/CAR), usada pela API e pelo worker da carteira."""

COLUNAS_IMOVEL = "id, tipo, fonte, codigo, nome, municipio, uf, situacao, area_ha, reserva_legal_ha"


def carregar_imovel(conn, imovel_id: int) -> dict | None:
    """Imóvel com a geometria em `geom_json` (GeoJSON em texto)."""
    return conn.execute(
        f"SELECT {COLUNAS_IMOVEL}, ST_AsGeoJSON(geom, 6) AS geom_json FROM geo.imoveis WHERE id = %s", (imovel_id,)
    ).fetchone()


def imovel_por_codigo(conn, codigo: str) -> dict | None:
    return conn.execute(
        f"""SELECT {COLUNAS_IMOVEL}, ST_AsGeoJSON(geom, 6) AS geom_json FROM geo.imoveis
            WHERE lower(codigo) = lower(%s) ORDER BY tipo = 'SIGEF' DESC LIMIT 1""", (codigo.strip(),)
    ).fetchone()
