"""Mercado de lotes: o que o comprador vê (sem identificar o vendedor) e o selo de verificação."""
from datetime import date, datetime, timezone

from app import lotes


def _linha(**extra):
    base = {
        "id": 7, "empresa_id": 2, "cultura": "soja", "safra": "2025/26", "volume_t": 1200, "preco_pedido": 121.5,
        "entrega": "FOB fazenda", "disponivel_ate": date(2027, 3, 31), "observacoes": None, "situacao": "ativo",
        "municipio": "Barreiras/BA", "area_ha": 812.4, "ultimo_status": "warn", "ultimo_protocolo": "GL-1",
        "analisado_em": datetime(2026, 11, 18, tzinfo=timezone.utc), "criado_em": datetime(2026, 11, 18, tzinfo=timezone.utc),
        "empresa_nome": "Cooperativa Secreta",
        "detalhes": {"checks": [
            {"id": "desm_pos_2008", "titulo": "Desmatamento após 22/07/2008", "status": "warn"},
            {"id": "imoveis", "titulo": "Imóveis cadastrados sobrepostos", "status": "info"},
            {"id": "renovabio", "titulo": "Elegibilidade RenovaBio", "status": "ok", "rotulo": "Elegível (uso do solo)"},
        ]},
    }
    base.update(extra)
    return base


REFS = {"soja": {"rotulo": "Soja", "unidade": "saca 60 kg", "preco_saca": 116.11, "fonte_preco": "IBGE/PAM 2025"}}


def test_lote_publico_nao_expoe_o_vendedor():
    p = lotes._lote_publico(_linha(), REFS, empresa_id=9)
    assert "empresa_nome" not in p and "empresa_id" not in p and "Cooperativa Secreta" not in str(p)
    assert p["meu"] is False and p["preco_referencia"] == 116.11 and p["area_ha"] == 812
    assert lotes._lote_publico(_linha(), REFS, empresa_id=2)["meu"] is True


def test_selo_do_lote():
    s = lotes._lote_publico(_linha(), REFS, empresa_id=9)["verificacao"]
    assert s["rotulo"] == "Atenção" and s["ocorrencias"] == ["Desmatamento após 22/07/2008"]
    assert s["renovabio"] == "Elegível (uso do solo)" and s["renovabio_elegivel"] is True
    sem = lotes._lote_publico(_linha(detalhes=None), REFS, empresa_id=9)["verificacao"]
    assert sem["ocorrencias"] == [] and sem["renovabio"] is None
