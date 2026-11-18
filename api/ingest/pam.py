"""Produção Agrícola Municipal (IBGE/PAM, tabela SIDRA 5457) dos municípios carregados em geo.municipios.

Valor, quantidade, área colhida e rendimento por cultura, do ano mais recente publicado. Daqui saem a produtividade e
o preço médio oficiais (valor / quantidade) que o GeoLEM usa por padrão no valor em risco e no observatório.
"""
import time

import httpx

SIDRA = "https://apisidra.ibge.gov.br/values/t/5457/n6/{municipios}/v/112,214,215,216/p/last/c782/{produtos}"
PRODUTOS = {"0": "total", "40124": "soja", "40122": "milho", "40099": "algodao", "40139": "cafe", "40125": "sorgo"}
VARIAVEIS = {"112": "rendimento_kg_ha", "214": "quantidade_t", "215": "valor_mil_reais", "216": "area_colhida_ha"}
CABECALHOS = {"User-Agent": "GeoLEM/0.3 (dados publicos IBGE)"}


def _numero(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):  # "-", "..", "X": sem dado ou sigilo
        return None


def ler(codigos: list[str]) -> tuple[int, list[dict]]:
    """Consulta o SIDRA em blocos de municípios. Devolve (ano, linhas por município e produto)."""
    linhas: dict[tuple[str, str], dict] = {}
    ano = None
    with httpx.Client(headers=CABECALHOS, timeout=120) as cliente:
        for i in range(0, len(codigos), 40):
            url = SIDRA.format(municipios=",".join(codigos[i:i + 40]), produtos=",".join(PRODUTOS))
            for n in range(1, 4):
                try:
                    r = cliente.get(url)
                    r.raise_for_status()
                    dados = r.json()
                    break
                except (httpx.HTTPError, ValueError):
                    if n == 3:
                        raise
                    time.sleep(5 * n)
            for d in dados[1:]:  # a primeira linha é o cabeçalho
                ano = int(d["D3C"])
                chave = (d["D1C"], PRODUTOS[d["D4C"]])
                linha = linhas.setdefault(chave, {"cod_ibge": d["D1C"], "produto": chave[1]})
                linha[VARIAVEIS[d["D2C"]]] = _numero(d["V"])
    return ano, list(linhas.values())


def carregar_pam(conn) -> int:
    codigos = [r["id_origem"] for r in conn.execute(
        "SELECT DISTINCT id_origem FROM geo.municipios WHERE id_origem IS NOT NULL ORDER BY 1").fetchall()]
    if not codigos:
        raise RuntimeError("Carregue os municípios antes (python -m ingest municipios_ibge).")
    carga_id = conn.execute(
        """INSERT INTO geo.cargas (fonte, tabela, descricao, url) VALUES ('pam_ibge', 'geo.producao_municipal',
           'Produção Agrícola Municipal (IBGE/PAM)', 'https://apisidra.ibge.gov.br · tabela 5457') RETURNING id""",
    ).fetchone()["id"]
    try:
        ano, linhas = ler(codigos)
        with conn.transaction():
            conn.execute("DELETE FROM geo.producao_municipal WHERE ano = %s", (ano,))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO geo.producao_municipal (cod_ibge, ano, produto, area_colhida_ha, quantidade_t,
                                                          rendimento_kg_ha, valor_mil_reais, carga_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(l["cod_ibge"], ano, l["produto"], l.get("area_colhida_ha"), l.get("quantidade_t"),
                      l.get("rendimento_kg_ha"), l.get("valor_mil_reais"), carga_id) for l in linhas])
            conn.execute("UPDATE geo.cargas SET status = 'ok', concluido_em = now(), registros = %s WHERE id = %s",
                         (len(linhas), carga_id))
        print(f"  ✓ pam_ibge: {len(linhas)} registros do ano {ano} para {len(codigos)} municípios")
        return len(linhas)
    except Exception as e:
        conn.execute("UPDATE geo.cargas SET status = 'erro', concluido_em = now(), erro = %s WHERE id = %s",
                     (str(e)[:2000], carga_id))
        print(f"  ✗ pam_ibge: {e}")
        raise
