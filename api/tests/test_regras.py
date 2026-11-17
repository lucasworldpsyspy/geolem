from datetime import date

from app import regras
from ingest import fontes


def test_desmatamento_pela_data_da_imagem():
    assert regras.classificar_desmatamento(2021, date(2020, 12, 31)) == "pos_2008"  # PRODES 2021 antes do corte
    assert regras.classificar_desmatamento(2021, date(2021, 1, 1)) == "pos_2020"
    assert regras.classificar_desmatamento(2008, date(2008, 7, 22)) == "consolidado"
    assert regras.classificar_desmatamento(2009, date(2008, 7, 23)) == "pos_2008"


def test_desmatamento_sem_data_usa_ano():
    assert regras.classificar_desmatamento(2022, None) == "pos_2020"
    assert regras.classificar_desmatamento(2015, None) == "pos_2008"
    assert regras.classificar_desmatamento(2005, None) == "consolidado"
    assert regras.classificar_desmatamento(None, None) == "pos_2008"


def test_reserva_legal():
    assert regras.avaliar_reserva_legal(None, 1000)[0] == "info"
    assert regras.avaliar_reserva_legal(200, 1000)[0] == "ok"
    st, texto = regras.avaliar_reserva_legal(150, 1000)
    assert st == "warn" and "Déficit: 50 ha" in texto


def test_status_geral():
    assert regras.status_geral([]) == "ok"
    assert regras.status_geral(["ok", "info"]) == "ok"
    assert regras.status_geral(["ok", "warn", "info"]) == "warn"
    assert regras.status_geral(["warn", "bad"]) == "bad"


def test_formatacao():
    assert regras.fmt_ha(1234.56) == "1.234,6 ha"
    assert regras.fmt_ha(50) == "50 ha"
    assert regras.fmt_pct(0.149) == "14,9%"


def test_grupo_uc():
    assert fontes.grupo_uc("PI") == "PI"
    assert fontes.grupo_uc(None, "Parque Estadual") == "PI"
    assert fontes.grupo_uc("Proteção Integral") == "PI"
    assert fontes.grupo_uc(None, "Área de Proteção Ambiental") == "US"


def test_datas_das_fontes():
    assert fontes.data(1609459200000) == date(2021, 1, 1)  # epoch ms do ArcGIS
    assert fontes.data("2014-08-10Z") == date(2014, 8, 10)
    assert fontes.data("") is None


def test_embargo_nao_guarda_dados_pessoais():
    linha = fontes.m_embargo({"num_tad": "123", "nome_embargado": "Fulano", "cpf_cnpj_embargado": "000"})
    assert "nome_embargado" not in linha and "cpf_cnpj_embargado" not in linha
    assert set(linha) == set(fontes.COLUNAS["geo.embargos"])
