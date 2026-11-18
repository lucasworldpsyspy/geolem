"""Elegibilidade RenovaBio: corte de 27/11/2018, CAR ativo/pendente e fora do resultado geral."""
from datetime import date

from app import laudo_pdf, regras
from tests.test_laudo_pdf import _resultado


def test_corte_renovabio():
    assert regras.CORTE_RENOVABIO == date(2018, 11, 27)
    assert regras.supressao_pos_renovabio(2019, date(2018, 11, 28))
    assert not regras.supressao_pos_renovabio(2019, date(2018, 11, 27))
    assert not regras.supressao_pos_renovabio(2019, date(2018, 8, 10))  # a data da imagem manda
    assert regras.supressao_pos_renovabio(2019, None)                   # ano PRODES 2019 inclui dez/2018 em diante
    assert not regras.supressao_pos_renovabio(2018, None)
    assert regras.supressao_pos_renovabio(None, None)                   # sem informação: conservador


def test_avaliacao_renovabio():
    st, rot, txt = regras.avaliar_renovabio(138.4, None, "SIGEF", 0.1)
    assert (st, rot) == ("bad", "Inelegível") and "138,4 ha" in txt
    assert regras.avaliar_renovabio(0.05, None, "SIGEF", 0.1)[:2] == ("ok", "Elegível (uso do solo)")
    assert regras.avaliar_renovabio(0, "Ativo", "CAR", 0.1)[:2] == ("ok", "Elegível")
    assert regras.avaliar_renovabio(0, "PENDENTE", "CAR", 0.1)[:2] == ("ok", "Elegível")
    assert regras.avaliar_renovabio(0, "Cancelado", "CAR", 0.1)[:2] == ("bad", "Inelegível")


def test_renovabio_nao_entra_no_resultado_geral():
    checks = [{"id": "desm_pos_2020", "status": "ok"}, {"id": "renovabio", "status": "bad"},
              {"id": "imoveis", "status": "info"}]
    assert regras.status_geral([c["status"] for c in checks if regras.entra_no_resultado(c)]) == "ok"


def test_pdf_mostra_rotulo_proprio():
    r = _resultado()
    r["checks"].append({"id": "renovabio", "titulo": "Elegibilidade RenovaBio (biomassa)", "base": "ANP 984/2025",
                        "status": "bad", "rotulo": "Inelegível", "ha": 138.4,
                        "itens": [{"descricao": "Supressão após 27/11/2018", "ha": None}]})
    assert laudo_pdf.gerar_pdf(r, "http://x/verificar/1").startswith(b"%PDF")
