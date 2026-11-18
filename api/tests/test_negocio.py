"""Licenças, observatório e boletim. Sem banco."""
from datetime import date

import pytest

from app import avisos, licencas, observatorio


def test_bloqueio_de_licenca():
    hoje = date(2026, 11, 17)
    assert licencas.motivo_bloqueio({"situacao": "ativa", "valido_ate": None}, hoje) is None
    assert licencas.motivo_bloqueio({"situacao": "teste", "valido_ate": date(2026, 11, 17)}, hoje) is None
    assert "teste" in licencas.motivo_bloqueio({"situacao": "teste", "valido_ate": date(2026, 11, 16)}, hoje)
    assert "venceu" in licencas.motivo_bloqueio({"situacao": "ativa", "valido_ate": date(2026, 1, 1)}, hoje)
    assert "suspenso" in licencas.motivo_bloqueio({"situacao": "suspensa", "valido_ate": None}, hoje)
    # A operadora (GeoLEM) nunca é bloqueada
    assert licencas.motivo_bloqueio({"situacao": "suspensa", "operadora": True}, hoje) is None


def test_planos():
    piloto = licencas.aplicar_plano("piloto", date(2026, 11, 17))
    assert piloto == {"limite_areas": 20, "limite_usuarios": 3, "limite_laudos_mes": 30, "valido_ate": date(2026, 12, 17)}
    assert licencas.aplicar_plano("corporativo") == {"limite_areas": None, "limite_usuarios": None, "limite_laudos_mes": None}
    with pytest.raises(licencas.ErroLicenca):
        licencas.aplicar_plano("ouro")


def test_chave_de_municipio_junta_grafias():
    assert observatorio.chave_municipio("SÃO DESIDÉRIO") == observatorio.chave_municipio("São Desidério")
    assert observatorio.chave_municipio("Luís Eduardo  Magalhães") == "LUIS EDUARDO MAGALHAES"
    assert observatorio.chave_municipio(None) == ""


def test_boletim_com_e_sem_alertas():
    com = avisos.montar_boletim("Barreiras", [{"data_deteccao": date(2026, 11, 6), "area_ha": 58.6},
                                              {"data_deteccao": date(2026, 11, 5), "area_ha": 3.9}], "a@b.com", "tk1")
    texto = com.get_content()
    assert "2 alerta(s)" in com["Subject"] and "62,5 ha" in texto and "06/11/2026" in texto
    assert "/api/publico/sair?token=tk1" in texto  # descadastro em todo e-mail
    sem = avisos.montar_boletim("Barreiras", [], "a@b.com", "tk2")
    assert "nenhum alerta" in sem["Subject"]
