"""Alertas de desmatamento: mapeamento do DETER e do MapBiomas, regra de data e cliente da API (simulada)."""
import json
from datetime import date

import httpx
import pytest

from app import regras
from ingest import clientes, fontes


def test_alerta_pos_corte():
    assert regras.alerta_pos_corte(date(2021, 1, 1))
    assert not regras.alerta_pos_corte(date(2020, 12, 31))
    assert regras.alerta_pos_corte(None)
    assert regras.STATUS_ALERTA == {"mapbiomas_alerta": "bad", "deter_cerrado": "warn"}


def test_mapeia_deter():
    d = fontes.m_deter({"gid": "72249_curr", "classname": "DESMATAMENTO_CR", "view_date": "2026-11-06",
                        "created_date": "2026-11-11", "publish_month": "2026-11-01", "areatotalkm": 0.5,
                        "satellite": "AMAZONIA-1", "sensor": "WFI"})
    assert d == {"id_origem": "72249_curr", "data_deteccao": date(2026, 11, 6), "data_publicacao": date(2026, 11, 1),
                 "area_ha": 50.0, "classe": "DESMATAMENTO_CR", "situacao": None, "satelite": "AMAZONIA-1 WFI"}
    assert set(d) == set(fontes.COLUNAS["geo.alertas_desmatamento"])


def test_mapeia_mapbiomas():
    d = fontes.m_mapbiomas({"alertCode": 1234567, "areaHa": 12.5, "detectedAt": "2025-08-10",
                            "publishedAt": "2025-09-01", "deforestationClasses": ["Desmatamento"],
                            "statusName": "published", "sources": ["Sentinel-2", "Planet"]})
    assert d["id_origem"] == "1234567" and d["data_deteccao"] == date(2025, 8, 10) and d["area_ha"] == 12.5
    assert d["classe"] == "Desmatamento" and d["satelite"] == "Sentinel-2, Planet"


def test_mapbiomas_exige_configuracao(monkeypatch):
    monkeypatch.delenv("MAPBIOMAS_EMAIL", raising=False)
    monkeypatch.delenv("MAPBIOMAS_SENHA", raising=False)
    assert not fontes.mapbiomas_configurado()
    with pytest.raises(RuntimeError, match="MAPBIOMAS_EMAIL"):
        list(fontes.FONTES["mapbiomas_alerta"].ler((-46, -13, -45, -12)))


def test_cliente_mapbiomas_login_paginas_e_geometria(monkeypatch):
    pedidos = []

    def responder(req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content)
        pedidos.append((corpo, req.headers.get("authorization")))
        if "signIn" in corpo["query"]:
            return httpx.Response(200, json={"data": {"signIn": {"token": "tk"}}})
        pagina = corpo["variables"]["p"]
        alerta = {"alertCode": pagina, "areaHa": 1.0, "detectedAt": "2024-05-01", "publishedAt": "2024-06-01",
                  "deforestationClasses": [], "statusName": "published", "sources": [],
                  "geometryWkt": "POLYGON((-45.8 -12.1, -45.7 -12.1, -45.7 -12.0, -45.8 -12.1))"}
        return httpx.Response(200, json={"data": {"alerts": {"collection": [alerta],
                                                              "metadata": {"totalPages": 2, "totalCount": 2}}}})

    cliente_real = httpx.Client
    monkeypatch.setattr(clientes.httpx, "Client",
                        lambda **kw: cliente_real(transport=httpx.MockTransport(responder), **kw))
    feicoes = list(clientes.mapbiomas_alertas("a@b.com", "x", (-46.7, -12.9, -45.0, -11.3)))
    assert [f["properties"]["alertCode"] for f in feicoes] == [1, 2]
    assert feicoes[0]["geometry"]["type"] == "Polygon" and "geometryWkt" not in feicoes[0]["properties"]
    assert pedidos[1][1] == "Bearer tk"  # consultas autenticadas com o token do login
    assert pedidos[1][0]["variables"]["b"] == [-46.7, -12.9, -45.0, -11.3]


def test_cliente_mapbiomas_login_recusado(monkeypatch):
    cliente_real = httpx.Client
    monkeypatch.setattr(clientes.httpx, "Client", lambda **kw: cliente_real(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"errors": [{"message": "Credenciais inválidas"}], "data": None})), **kw))
    with pytest.raises(RuntimeError, match="recusou o login"):
        list(clientes.mapbiomas_alertas("a@b.com", "x", (0, 0, 1, 1)))
