"""Commodities, preço e produtividade de referência, e o "valor em risco" da carteira.

Ordem de preferência da referência de cada commodity:
1. o que o administrador cadastrou no Admin (com fonte e data), se o preço tiver até 60 dias;
2. a média oficial da região na Produção Agrícola Municipal do IBGE (valor ÷ quantidade, rendimento ponderado).
O valor em risco é uma estimativa de ordem de grandeza e nunca entra no laudo, que precisa ser reprodutível.
"""
import unicodedata
from datetime import date

# commodity -> (rótulo, unidade de preço, kg por unidade)
CULTURAS = {
    "soja": ("Soja", "saca 60 kg", 60),
    "milho": ("Milho", "saca 60 kg", 60),
    "sorgo": ("Sorgo", "saca 60 kg", 60),
    "algodao": ("Algodão (em caroço)", "arroba 15 kg", 15),
    "cafe": ("Café", "saca 60 kg", 60),
}
PRODUTOS = tuple(CULTURAS)
# Grão que pode virar biocombustível no RenovaBio (etanol de milho e sorgo, biodiesel de soja)
CULTURAS_RENOVABIO = ("milho", "sorgo", "soja")
VALIDADE_PRECO_DIAS = 60


def normalizar_cultura(texto: str | None) -> str:
    """'Algodão', 'ALGODAO', 'café arábica' -> chave interna; vazio -> soja (cultura dominante da região)."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().strip().lower()
    if not t:
        return "soja"
    for chave in CULTURAS:
        if t.startswith(chave):
            return chave
    raise ValueError(f"Cultura desconhecida: {texto}. Use soja, milho, sorgo, algodão ou café.")


def _pam_regional(conn) -> dict[str, dict]:
    """Média da região na PAM: preço (R$/kg) e rendimento (kg/ha) ponderados pelo volume."""
    linhas = conn.execute(
        """SELECT produto, max(ano) AS ano, sum(valor_mil_reais) * 1000 / nullif(sum(quantidade_t) * 1000, 0) AS reais_kg,
                  sum(quantidade_t) * 1000 / nullif(sum(area_colhida_ha), 0) AS kg_ha
           FROM geo.producao_municipal
           WHERE produto <> 'total' AND quantidade_t > 0 AND valor_mil_reais > 0
             AND ano = (SELECT max(ano) FROM geo.producao_municipal)
           GROUP BY produto""").fetchall()
    return {l["produto"]: l for l in linhas}


def referencias(conn) -> dict[str, dict]:
    pam = _pam_regional(conn)
    cadastradas = {l["produto"]: l for l in conn.execute(
        """SELECT produto, praca, preco_saca, data_preco, fonte_preco, produtividade_sc_ha, fonte_produtividade,
                  atualizado_em FROM app.referencias_mercado""").fetchall()}
    saida = {}
    for produto, (rotulo, unidade, kg) in CULTURAS.items():
        c = cadastradas.get(produto) or {}
        p = pam.get(produto)
        dias = (date.today() - c["data_preco"]).days if c.get("data_preco") else None
        if c.get("preco_saca") is not None and dias is not None and dias <= VALIDADE_PRECO_DIAS:
            preco, fonte_preco, data_preco = float(c["preco_saca"]), c.get("fonte_preco"), c["data_preco"].isoformat()
        elif p and p["reais_kg"]:
            preco = round(float(p["reais_kg"]) * kg, 2)
            fonte_preco, data_preco, dias = f"IBGE/PAM {p['ano']}, preço médio da região (valor ÷ quantidade)", None, None
        else:
            preco = float(c["preco_saca"]) if c.get("preco_saca") is not None else None
            fonte_preco = c.get("fonte_preco")
            data_preco = c["data_preco"].isoformat() if c.get("data_preco") else None
        if c.get("produtividade_sc_ha"):
            prod, fonte_prod = float(c["produtividade_sc_ha"]), c.get("fonte_produtividade")
        elif p and p["kg_ha"]:
            prod, fonte_prod = round(float(p["kg_ha"]) / kg, 1), f"IBGE/PAM {p['ano']}, rendimento médio da região"
        else:
            prod, fonte_prod = None, None
        saida[produto] = {
            "produto": produto, "rotulo": rotulo, "unidade": unidade, "kg_por_unidade": kg,
            "praca": c.get("praca") or "Região do GeoLEM", "preco_saca": preco, "fonte_preco": fonte_preco,
            "data_preco": data_preco, "dias_do_preco": dias,
            "produtividade_sc_ha": prod, "fonte_produtividade": fonte_prod,
            "preco_cadastrado": float(c["preco_saca"]) if c.get("preco_saca") is not None else None,
            "data_cadastrada": c["data_preco"].isoformat() if c.get("data_preco") else None,
            "fonte_cadastrada": c.get("fonte_preco"),
            "produtividade_cadastrada": float(c["produtividade_sc_ha"]) if c.get("produtividade_sc_ha") else None,
            "fonte_produtividade_cadastrada": c.get("fonte_produtividade"),
            "atualizado_em": c["atualizado_em"].isoformat() if c.get("atualizado_em") else None,
        }
    return saida


def estimar(ha: float, ref: dict | None) -> dict:
    """Produção anual estimada de uma área: unidades (sacas ou arrobas), toneladas e reais (se houver preço)."""
    if not ref or not ref.get("produtividade_sc_ha"):
        return {"ha": round(ha, 1), "sacas": None, "toneladas": None, "reais": None}
    unidades = ha * ref["produtividade_sc_ha"]
    kg = ref.get("kg_por_unidade", 60)
    return {"ha": round(ha, 1), "sacas": round(unidades), "toneladas": round(unidades * kg / 1000, 1),
            "reais": round(unidades * ref["preco_saca"], 2) if ref.get("preco_saca") else None}


def _somar(partes: list[dict]) -> dict:
    tem = [p for p in partes if p["toneladas"] is not None]
    return {"ha": round(sum(p["ha"] for p in partes), 1),
            "sacas": None,  # unidades diferentes entre culturas: a soma só faz sentido em t e R$
            "toneladas": round(sum(p["toneladas"] for p in tem), 1) if tem else None,
            "reais": round(sum(p["reais"] for p in tem if p["reais"] is not None), 2) if tem else None}


def risco_carteira(conn, empresa_id: int) -> dict:
    """Produção anual da carteira em fornecedores Não conformes e o grão inelegível no RenovaBio, pela cultura de
    cada área. Dois recortes: a área toda do fornecedor e só os hectares com ocorrência."""
    refs = referencias(conn)
    areas = conn.execute(
        """SELECT a.cultura, a.area_ha, a.ultimo_status,
                  (SELECT max((c->>'ha')::numeric) FROM jsonb_array_elements(an.detalhes->'checks') c
                   WHERE c->>'status' = 'bad' AND c->>'id' NOT IN ('renovabio', 'imoveis')) AS ha_ocorrencia,
                  (SELECT (c->>'ha')::numeric FROM jsonb_array_elements(an.detalhes->'checks') c
                   WHERE c->>'id' = 'renovabio' AND c->>'status' = 'bad') AS ha_renovabio
           FROM app.areas a LEFT JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo
           WHERE a.empresa_id = %s AND a.ativa""", (empresa_id,)).fetchall()
    carteira, fornecedores, afetada, renovabio, por_cultura = [], [], [], [], {}
    for a in areas:
        ref = refs.get(a["cultura"])
        carteira.append(estimar(float(a["area_ha"] or 0), ref))
        if a["ultimo_status"] == "bad":
            e = estimar(float(a["area_ha"] or 0), ref)
            fornecedores.append(e)
            afetada.append(estimar(float(a["ha_ocorrencia"] or 0), ref))
            pc = por_cultura.setdefault(a["cultura"], [])
            pc.append(e)
        if a["ha_renovabio"] and a["cultura"] in CULTURAS_RENOVABIO:
            renovabio.append(estimar(float(a["ha_renovabio"]), ref))
    return {
        "referencias": refs,
        "carteira": _somar(carteira),
        "eudr_fornecedores": _somar(fornecedores),
        "eudr_area_afetada": _somar(afetada),
        "renovabio_inelegivel": _somar(renovabio),
        "por_cultura": {c: _somar(v) for c, v in por_cultura.items()},
    }
