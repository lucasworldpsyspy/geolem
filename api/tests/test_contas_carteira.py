"""Contas (senhas, limites de tentativa), planilha da carteira e e-mail de alertas. Sem banco."""
import io

import pytest
from openpyxl import Workbook

from app import avisos, contas
from app.planilha import ErroPlanilha, ler_planilha


# ---------- senhas ----------
def test_hash_e_conferencia_de_senha():
    h = contas.hash_senha("Senha-forte-123")
    assert h.startswith("scrypt$") and "Senha-forte-123" not in h
    assert contas.conferir_senha("Senha-forte-123", h)
    assert not contas.conferir_senha("senha-forte-123", h)
    assert contas.hash_senha("Senha-forte-123") != h  # sal diferente a cada vez
    assert not contas.conferir_senha("x", "lixo")


def test_validacao_de_senha():
    for ruim in ("curta1", "somenteletras", "12345678901"):
        with pytest.raises(contas.ErroConta):
            contas.validar_senha(ruim)
    contas.validar_senha("Cerrado2026ok")


def test_senha_provisoria_valida_e_sem_ambiguos():
    for _ in range(50):
        s = contas.senha_provisoria()
        contas.validar_senha(s)
        assert not set(s) & set("0O1lI")


def test_email():
    assert contas.normalizar_email("  Maria@Exemplo.COM.br ") == "maria@exemplo.com.br"
    with pytest.raises(contas.ErroConta):
        contas.normalizar_email("maria@exemplo")


def test_limite_de_tentativas():
    lim = contas.LimiteTentativas(3, 60)
    for _ in range(3):
        assert not lim.bloqueado("ip")
        lim.falhou("ip")
    assert lim.bloqueado("ip")
    lim.limpar("ip")
    assert not lim.bloqueado("ip")


# ---------- planilha ----------
def test_planilha_csv_ponto_e_virgula_cp1252():
    csv = ("Código;Fazenda;Produtor;Código ERP;Reserva Legal (ha)\n"
           "abc-123;Fazenda São João;José;F10;1.234,5\n"
           ";Sem código;;;\n").encode("cp1252")
    linhas = ler_planilha("carteira.csv", csv)
    assert linhas[0] == {"linha": 2, "codigo": "abc-123", "nome": "Fazenda São João", "fornecedor": "José",
                         "codigo_externo": "F10", "cultura": None, "reserva_legal_ha": 1234.5}
    assert linhas[1]["codigo"] is None


def test_planilha_csv_virgula_utf8_com_bom():
    linhas = ler_planilha("c.csv", "﻿sigef,nome\nxyz,Boa Vista\n".encode("utf-8"))
    assert linhas == [{"linha": 2, "codigo": "xyz", "nome": "Boa Vista", "fornecedor": None, "codigo_externo": None,
                       "cultura": None, "reserva_legal_ha": None}]


def test_planilha_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.append(["codigo", "fornecedor", "reserva_legal_ha"])
    ws.append(["5ba5ff8d", "Maria", 610])
    ws.append([None, None, None])  # linha vazia é ignorada
    buf = io.BytesIO()
    wb.save(buf)
    linhas = ler_planilha("c.xlsx", buf.getvalue())
    assert len(linhas) == 1 and linhas[0]["codigo"] == "5ba5ff8d" and linhas[0]["reserva_legal_ha"] == 610


def test_planilha_erros():
    with pytest.raises(ErroPlanilha, match="codigo"):
        ler_planilha("c.csv", b"nome;fornecedor\nA;B\n")
    with pytest.raises(ErroPlanilha):
        ler_planilha("c.pdf", b"%PDF")
    with pytest.raises(ErroPlanilha, match="vazia"):
        ler_planilha("c.csv", b"codigo\n")
    linhas = ler_planilha("c.csv", b"codigo;reserva_legal\nA;muito\n")
    assert "erro" in linhas[0]


# ---------- e-mail de alertas ----------
def test_email_de_alertas():
    msg = avisos.montar_email("Armazém X", [
        {"nome": "Fazenda A", "fornecedor": "José", "de": "ok", "para": "bad", "protocolo": "GL-1",
         "resumo": "Desmatamento após 31/12/2020 (12 ha)"},
    ], ["admin@x.com.br"])
    corpo = msg.get_content()
    assert "1 fornecedor(es)" in msg["Subject"]
    assert "Fazenda A (José): Conforme → Não conforme" in corpo
    assert "/api/analises/GL-1/laudo.pdf" in corpo and msg["To"] == "admin@x.com.br"
