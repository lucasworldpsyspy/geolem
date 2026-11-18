"""Preço e produtividade de referência, e o "valor em risco" da carteira.

O preço vem de uma tabela gerida no Admin, sempre com fonte e data: o valor em risco é uma estimativa para dar
ordem de grandeza (em reais e sacas), nunca entra no laudo, que precisa ser reprodutível.
"""
from datetime import date

PRODUTOS = ("soja", "milho")
SACA_KG = 60


def referencias(conn) -> dict[str, dict]:
    linhas = conn.execute(
        """SELECT produto, praca, preco_saca, data_preco, fonte_preco, produtividade_sc_ha, fonte_produtividade,
                  atualizado_em FROM app.referencias_mercado""").fetchall()
    saida = {}
    for l in linhas:
        saida[l["produto"]] = {
            **l,
            "preco_saca": float(l["preco_saca"]) if l["preco_saca"] is not None else None,
            "produtividade_sc_ha": float(l["produtividade_sc_ha"]) if l["produtividade_sc_ha"] is not None else None,
            "data_preco": l["data_preco"].isoformat() if l["data_preco"] else None,
            "atualizado_em": l["atualizado_em"].isoformat(),
            "dias_do_preco": (date.today() - l["data_preco"]).days if l["data_preco"] else None,
        }
    return saida


def estimar(ha: float, ref: dict | None) -> dict:
    """Produção anual estimada de uma área: sacas, toneladas e reais (se houver preço)."""
    if not ref or not ref.get("produtividade_sc_ha"):
        return {"ha": round(ha, 1), "sacas": None, "toneladas": None, "reais": None}
    sacas = ha * ref["produtividade_sc_ha"]
    return {"ha": round(ha, 1), "sacas": round(sacas), "toneladas": round(sacas * SACA_KG / 1000, 1),
            "reais": round(sacas * ref["preco_saca"], 2) if ref.get("preco_saca") else None}


def risco_carteira(conn, empresa_id: int) -> dict:
    """Quanto da produção anual da carteira está em fornecedores Não conformes (soja) e quanto milho é inelegível
    no RenovaBio. Dois recortes: a área toda do fornecedor e só os hectares com ocorrência."""
    refs = referencias(conn)
    r = conn.execute(
        """SELECT coalesce(sum(a.area_ha) FILTER (WHERE a.ultimo_status = 'bad'), 0) AS ha_fornecedores_bad,
                  coalesce(sum(a.area_ha), 0) AS ha_carteira,
                  coalesce(sum((SELECT max((c->>'ha')::numeric) FROM jsonb_array_elements(an.detalhes->'checks') c
                                WHERE c->>'status' = 'bad' AND c->>'id' NOT IN ('renovabio', 'imoveis')))
                           FILTER (WHERE a.ultimo_status = 'bad'), 0) AS ha_com_ocorrencia,
                  coalesce(sum((SELECT (c->>'ha')::numeric FROM jsonb_array_elements(an.detalhes->'checks') c
                                WHERE c->>'id' = 'renovabio' AND c->>'status' = 'bad')), 0) AS ha_renovabio
           FROM app.areas a LEFT JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo
           WHERE a.empresa_id = %s AND a.ativa""", (empresa_id,)).fetchone()
    soja, milho = refs.get("soja"), refs.get("milho")
    return {
        "referencias": refs,
        "carteira": estimar(float(r["ha_carteira"]), soja),
        "eudr_fornecedores": estimar(float(r["ha_fornecedores_bad"]), soja),
        "eudr_area_afetada": estimar(float(r["ha_com_ocorrencia"]), soja),
        "renovabio_inelegivel": estimar(float(r["ha_renovabio"]), milho),
    }
