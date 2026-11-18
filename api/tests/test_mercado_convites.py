"""Valor em risco e convite ao produtor (partes sem banco)."""
from app import convites, mercado


def test_estimativa_de_producao():
    soja = {"produtividade_sc_ha": 71, "preco_saca": 134.0}
    e = mercado.estimar(100, soja)
    assert e == {"ha": 100, "sacas": 7100, "toneladas": 426.0, "reais": 951400.0}
    sem_preco = mercado.estimar(10, {"produtividade_sc_ha": 187, "preco_saca": None})
    assert sem_preco["sacas"] == 1870 and sem_preco["toneladas"] == 112.2 and sem_preco["reais"] is None
    assert mercado.estimar(10, None)["sacas"] is None


def test_mensagem_e_link_do_convite():
    msg = convites.mensagem("Cooperativa X", "Fazenda Boa Vista", "abc123")
    assert "Cooperativa X" in msg and "Fazenda Boa Vista" in msg
    assert convites.link("abc123").endswith("/convite.html?t=abc123")
    assert "Posso pedir a exclusão" in convites.CONSENTIMENTO


def test_culturas():
    assert mercado.normalizar_cultura("Algodão") == "algodao"
    assert mercado.normalizar_cultura(" CAFÉ arábica") == "cafe"
    assert mercado.normalizar_cultura(None) == "soja"
    import pytest
    with pytest.raises(ValueError):
        mercado.normalizar_cultura("trigo")
    algodao = {"produtividade_sc_ha": 300, "preco_saca": 100.0, "kg_por_unidade": 15}
    assert mercado.estimar(10, algodao) == {"ha": 10, "sacas": 3000, "toneladas": 45.0, "reais": 300000.0}
