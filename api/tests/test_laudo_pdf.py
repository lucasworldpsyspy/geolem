from app import laudo_pdf

QUADRADO = {"type": "Polygon", "coordinates": [[[-45.8, -12.1], [-45.78, -12.1], [-45.78, -12.08], [-45.8, -12.08],
                                                 [-45.8, -12.1]]]}


def _resultado():
    return {
        "protocolo": "GL-20261117-ABCD1234", "criado_em": "2026-11-17T20:00:00+00:00",
        "area": {"nome": "FAZENDA SÃO JOÃO", "area_ha": 480.5, "ponto_interno": [-45.79, -12.09],
                 "imovel": {"tipo": "SIGEF", "codigo": "abc", "municipio": "São Desidério", "uf": "BA"},
                 "geometria": QUADRADO},
        "geral": "bad", "rotulo_geral": "Não conforme", "resumo": "Há impedimentos.",
        "checks": [{"id": "embargos", "titulo": "Embargos ambientais", "base": "IBAMA", "status": "bad", "ha": 1.2,
                    "itens": [{"descricao": "TAD 1-C de 12/09/2007: Destruir <vegetação> & afins", "ha": 1.2}]}],
        "conflitos": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": QUADRADO, "properties": {"status": "bad"}}]},
        "bases": [{"descricao": "Áreas embargadas (IBAMA)", "url": "https://x", "atualizado_em": None, "registros": 165}],
        "parametros": {"area_minima_ha": 0.1},
    }


def test_gera_pdf_valido():
    pdf = laudo_pdf.gerar_pdf(_resultado(), "http://localhost:8000/verificar/GL-20261117-ABCD1234")
    assert pdf.startswith(b"%PDF") and len(pdf) > 3000


def test_integridade_estavel_e_sensivel():
    r = _resultado()
    h = laudo_pdf.codigo_integridade(r)
    assert len(h) == 64
    assert laudo_pdf.codigo_integridade({**r, "integridade": h, "integridade_conferida": True}) == h
    assert laudo_pdf.codigo_integridade({**r, "geral": "ok"}) != h


def test_area_sem_geometria_nao_quebra():
    r = _resultado()
    r["area"]["geometria"] = {"type": "Polygon", "coordinates": []}
    r["conflitos"]["features"] = []
    assert laudo_pdf.gerar_pdf(r, "http://x/verificar/1").startswith(b"%PDF")
