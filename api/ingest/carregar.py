"""Carrega uma fonte no PostGIS, substituindo a carga anterior da mesma fonte numa única transação."""
import json
import time

import psycopg
from psycopg.rows import dict_row

from .fontes import COLUNAS, Fonte

TIPOS_POLIGONO = {"Polygon", "MultiPolygon"}


def conectar(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def carregar(conn: psycopg.Connection, fonte: Fonte, bbox: tuple) -> int:
    inicio = time.time()
    carga_id = conn.execute(
        "INSERT INTO geo.cargas (fonte, tabela, descricao, url) VALUES (%s, %s, %s, %s) RETURNING id",
        (fonte.id, fonte.tabela, fonte.descricao, fonte.url),
    ).fetchone()["id"]

    try:
        colunas = COLUNAS[fonte.tabela]
        linhas, ignoradas = [], 0
        for f in fonte.ler(bbox):
            geom = f.get("geometry")
            if not geom or geom.get("type") not in TIPOS_POLIGONO:
                ignoradas += 1
                continue
            dados = fonte.mapear(f.get("properties") or {})
            if dados is None:
                ignoradas += 1
                continue
            linhas.append((fonte.id, carga_id, *[dados.get(c) for c in colunas], json.dumps(geom)))

        marcadores = ", ".join(["%s"] * (len(colunas) + 2))
        sql = (f"INSERT INTO {fonte.tabela} (fonte, carga_id, {', '.join(colunas)}, geom) "
               f"VALUES ({marcadores}, geo.normalizar(ST_GeomFromGeoJSON(%s)))")

        with conn.transaction():
            conn.execute(f"DELETE FROM {fonte.tabela} WHERE fonte = %s", (fonte.id,))
            with conn.cursor() as cur:
                cur.executemany(sql, linhas)
            conn.execute(f"DELETE FROM {fonte.tabela} WHERE carga_id = %s AND ST_IsEmpty(geom)", (carga_id,))
            if fonte.tabela == "geo.imoveis":
                conn.execute("UPDATE geo.imoveis SET area_ha = ST_Area(geom::geography) / 10000.0 "
                             "WHERE carga_id = %s", (carga_id,))
            total = conn.execute(f"SELECT count(*) AS n FROM {fonte.tabela} WHERE carga_id = %s",
                                 (carga_id,)).fetchone()["n"]
            conn.execute("UPDATE geo.cargas SET status = 'ok', concluido_em = now(), registros = %s WHERE id = %s",
                         (total, carga_id))
        print(f"  ✓ {fonte.id}: {total} registros ({ignoradas} ignorados) em {time.time() - inicio:.0f}s")
        return total
    except Exception as e:
        conn.execute("UPDATE geo.cargas SET status = 'erro', concluido_em = now(), erro = %s WHERE id = %s",
                     (str(e)[:2000], carga_id))
        print(f"  ✗ {fonte.id}: {e}")
        raise
