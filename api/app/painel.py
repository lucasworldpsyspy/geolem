"""Dashboard da carteira: evolução semanal, motivos das ocorrências, município, fornecedor, validade e mapa."""
import json

SEMANAS = 12


def painel(conn, empresa_id: int) -> dict:
    p = {"empresa_id": empresa_id}
    evolucao = conn.execute(
        f"""WITH s AS (SELECT ini::date AS ini, (ini + interval '7 days') AS fim
                       FROM generate_series(date_trunc('week', now()) - interval '{SEMANAS - 1} weeks',
                                            date_trunc('week', now()), interval '1 week') AS ini)
            SELECT s.ini AS semana,
                   count(*) FILTER (WHERE x.resultado = 'ok') AS ok,
                   count(*) FILTER (WHERE x.resultado = 'warn') AS warn,
                   count(*) FILTER (WHERE x.resultado = 'bad') AS bad,
                   count(*) FILTER (WHERE x.resultado IS NULL) AS sem_analise
            FROM s
            LEFT JOIN app.areas a ON a.empresa_id = %(empresa_id)s AND a.criado_em < s.fim
                 AND (a.ativa OR EXISTS (SELECT 1 FROM geo.analises z WHERE z.area_id = a.id AND z.criado_em >= s.fim))
            LEFT JOIN LATERAL (SELECT an.resultado FROM geo.analises an
                               WHERE an.area_id = a.id AND an.criado_em < s.fim
                               ORDER BY an.criado_em DESC LIMIT 1) x ON true
            GROUP BY s.ini ORDER BY s.ini""", p).fetchall()
    # Semanas sem nenhuma área ficam zeradas (o LEFT JOIN conta uma linha nula)
    for e in evolucao:
        e["semana"] = e["semana"].isoformat()
        if e["ok"] + e["warn"] + e["bad"] == 0 and e["sem_analise"] == 1:
            e["sem_analise"] = 0

    motivos = conn.execute(
        """SELECT c->>'titulo' AS titulo, c->>'status' AS status, count(*) AS areas,
                  round(sum(coalesce((c->>'ha')::numeric, 0)), 1) AS ha
           FROM app.areas a JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo
           CROSS JOIN LATERAL jsonb_array_elements(an.detalhes->'checks') c
           WHERE a.empresa_id = %(empresa_id)s AND a.ativa AND c->>'status' IN ('bad', 'warn')
             AND c->>'id' NOT IN ('imoveis', 'renovabio')
           GROUP BY 1, 2 ORDER BY (c->>'status' = 'bad') DESC, count(*) DESC""", p).fetchall()

    municipios = conn.execute(
        """SELECT coalesce(municipio, 'Não informado') AS municipio, count(*) AS areas,
                  count(*) FILTER (WHERE ultimo_status = 'bad') AS bad,
                  count(*) FILTER (WHERE ultimo_status = 'warn') AS warn,
                  count(*) FILTER (WHERE ultimo_status = 'ok') AS ok,
                  round(sum(area_ha)::numeric, 0) AS ha
           FROM app.areas WHERE empresa_id = %(empresa_id)s AND ativa
           GROUP BY 1 ORDER BY count(*) FILTER (WHERE ultimo_status = 'bad') DESC, count(*) DESC""", p).fetchall()

    fornecedores = conn.execute(
        """SELECT coalesce(nullif(fornecedor, ''), '(sem fornecedor informado)') AS fornecedor, count(*) AS areas,
                  count(*) FILTER (WHERE ultimo_status = 'bad') AS bad,
                  count(*) FILTER (WHERE ultimo_status = 'warn') AS warn,
                  round(sum(area_ha)::numeric, 0) AS ha,
                  round(coalesce(sum(area_ha) FILTER (WHERE ultimo_status = 'bad'), 0)::numeric, 0) AS ha_bad
           FROM app.areas WHERE empresa_id = %(empresa_id)s AND ativa
           GROUP BY 1 ORDER BY count(*) FILTER (WHERE ultimo_status = 'bad') DESC,
                              count(*) FILTER (WHERE ultimo_status = 'warn') DESC, count(*) DESC
           LIMIT 15""", p).fetchall()

    validade = conn.execute(
        """SELECT count(*) FILTER (WHERE analisado_em >= now() - interval '7 days') AS ate_7,
                  count(*) FILTER (WHERE analisado_em < now() - interval '7 days'
                                     AND analisado_em >= now() - interval '30 days') AS ate_30,
                  count(*) FILTER (WHERE analisado_em < now() - interval '30 days'
                                     AND analisado_em >= now() - interval '90 days') AS ate_90,
                  count(*) FILTER (WHERE analisado_em < now() - interval '90 days') AS mais_90,
                  count(*) FILTER (WHERE analisado_em IS NULL) AS nunca
           FROM app.areas WHERE empresa_id = %(empresa_id)s AND ativa""", p).fetchone()

    def numeros(linhas):
        return [{k: (float(v) if hasattr(v, "is_finite") else v) for k, v in l.items()} for l in linhas]

    return {"evolucao": evolucao, "motivos": numeros(motivos), "municipios": numeros(municipios),
            "fornecedores": numeros(fornecedores), "validade": validade}


def mapa(conn, empresa_id: int) -> dict:
    linhas = conn.execute(
        """SELECT id, nome, fornecedor, ultimo_status, ultimo_protocolo, round(area_ha::numeric, 1) AS area_ha,
                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.00005), 6) AS g
           FROM app.areas WHERE empresa_id = %s AND ativa""", (empresa_id,)).fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": l["id"], "geometry": json.loads(l["g"]),
         "properties": {"id": l["id"], "nome": l["nome"], "fornecedor": l["fornecedor"],
                        "status": l["ultimo_status"] or "fila", "protocolo": l["ultimo_protocolo"],
                        "area_ha": float(l["area_ha"]) if l["area_ha"] is not None else None}}
        for l in linhas]}
