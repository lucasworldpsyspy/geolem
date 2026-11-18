"""Territórios do INCRA, imagens de satélite (sem rede) e exportação EUDR."""
import hashlib
import math
from datetime import date
from io import BytesIO

from PIL import Image

from app import eudr, imagens, laudo_pdf, regras
from ingest import fontes
from tests.test_laudo_pdf import QUADRADO, _resultado


# ---------- territórios ----------
def test_status_quilombo():
    assert regras.status_quilombo("DECRETO") == "bad"
    assert regras.status_quilombo("TITULO PARCIAL") == "bad"
    assert regras.status_quilombo("Portaria publicada") == "bad"
    assert regras.status_quilombo("RTID") == "warn"
    assert regras.status_quilombo(None) == "warn"


def test_repara_acentos_quebrados_do_incra():
    assert fontes.texto("FAM�\udc8dLIA MAGALHÃES") == "FAMÍLIA MAGALHÃES"
    assert fontes.texto("PA �\udc81GUAS CLARAS") == "PA ÁGUAS CLARAS"
    assert fontes.texto("PA �\u0081GUAS") == "PA ÁGUAS"
    assert fontes.texto("  SÃO JOÃO ") == "SÃO JOÃO"


def test_mapeadores_incra():
    q = fontes.m_quilombo({"cd_quilomb": 7, "nm_comunid": "LAJEADO", "nm_municip": "DIANÓPOLIS", "cd_uf": "TO",
                           "fase": "RTID", "nr_familia": "40"})
    assert q == {"id_origem": "7", "tipo": "quilombola", "nome": "LAJEADO", "municipio": "DIANÓPOLIS", "uf": "TO",
                 "fase": "RTID", "familias": 40}
    a = fontes.m_assentamento({"cd_sipra": "BA0095000", "nome_proje": "PA RIO DE ONDAS", "municipio": "BARREIRAS",
                               "uf": "BA", "fase": 3, "descricao_": None, "num_famili": 242})
    assert a["tipo"] == "assentamento" and a["fase"] == "3" and a["familias"] == 242
    assert set(q) == set(fontes.COLUNAS["geo.territorios"])


# ---------- imagens ----------
def test_ultimo_periodo_seco():
    assert imagens.ultimo_periodo_seco(date(2026, 11, 17)) == 2026
    assert imagens.ultimo_periodo_seco(date(2026, 8, 1)) == 2025


def _proporcao_real(b):
    kx = math.cos(math.radians((b[1] + b[3]) / 2))
    return (b[2] - b[0]) * kx / (b[3] - b[1])


def test_janela_tem_margem_e_proporcao_limitada():
    area = (-45.68, -12.40, -45.64, -12.30)  # imóvel comprido no sentido norte-sul
    b = imagens.janela(area)
    assert b[0] < area[0] and b[1] < area[1] and b[2] > area[2] and b[3] > area[3]
    assert 0.74 <= _proporcao_real(b) <= 1.34
    pequena = imagens.janela((-45.8, -12.1, -45.7999, -12.0999))
    assert pequena[2] - pequena[0] > 0.006
    w, h = imagens.tamanho_px(b)
    assert max(w, h) <= 900 and abs(w / h - _proporcao_real(b)) < 0.02


def _png(w, h):
    buf = BytesIO()
    Image.new("RGB", (w, h), (90, 120, 60)).save(buf, "PNG")
    return buf.getvalue()


def _com_imagens(resultado, png, sha=None):
    meta = {"tipo": "antes", "titulo": "Antes", "bbox": [-45.81, -12.11, -45.77, -12.07], "largura_px": 300,
            "altura_px": 300, "data": "2020-09-01", "satelite": "Sentinel-2B", "nuvens_pct": 0.0,
            "sha256": sha or hashlib.sha256(png).hexdigest(), "atribuicao": imagens.ATRIBUICAO}
    resultado["imagens"] = [meta, {"tipo": "depois", "titulo": "Depois", "bbox": meta["bbox"],
                                   "indisponivel": "Nenhuma cena sem nuvens."}]
    return resultado


def test_pdf_com_imagens():
    png = _png(300, 300)
    sem = laudo_pdf.gerar_pdf(_resultado(), "http://x/verificar/1")
    com = laudo_pdf.gerar_pdf(_com_imagens(_resultado(), png), "http://x/verificar/1", {"antes": png})
    assert com.startswith(b"%PDF") and len(com) > len(sem)


def test_pdf_ignora_imagem_adulterada():
    png = _png(300, 300)
    r = _com_imagens(_resultado(), png, sha="0" * 64)
    pdf = laudo_pdf.gerar_pdf(r, "http://x/verificar/1", {"antes": png})
    assert pdf.startswith(b"%PDF")
    assert len(pdf) < len(laudo_pdf.gerar_pdf(_com_imagens(_resultado(), png), "http://x/verificar/1", {"antes": png}))


# ---------- EUDR ----------
def test_geojson_eudr():
    r = _resultado()
    r["area"]["geometria"] = {"type": "MultiPolygon", "coordinates": [[[[-45.81234567, -12.1], [-45.78, -12.1],
                                                                         [-45.78, -12.08], [-45.81234567, -12.1]]]]}
    fc = eudr.geojson_eudr(r)
    f = fc["features"][0]
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 1
    assert f["geometry"]["type"] == "Polygon"
    assert f["geometry"]["coordinates"][0][0] == [-45.812346, -12.1]
    assert f["properties"] == {"ProductionPlace": "FAZENDA SÃO JOÃO (SIGEF abc)", "ProducerCountry": "BR",
                               "Area": 480.5}


def test_geojson_eudr_multipoligono_mantido():
    r = _resultado()
    r["area"]["geometria"] = {"type": "MultiPolygon", "coordinates": [QUADRADO["coordinates"], QUADRADO["coordinates"]]}
    assert eudr.geojson_eudr(r)["features"][0]["geometry"]["type"] == "MultiPolygon"
