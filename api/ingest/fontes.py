"""Catálogo das bases públicas carregadas pelo GeoLEM.

Para adicionar uma base: crie um `Fonte` com o leitor (WFS ou ArcGIS) e a função que
mapeia os atributos de origem para as colunas da tabela de destino.
"""
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import clientes

TERRABRASILIS = "https://terrabrasilis.dpi.inpe.br/geoserver/ows"
IBAMA = "https://pamgia.ibama.gov.br/server/rest/services/01_Publicacoes_Bases"
IBAMA_SINC = "https://pamgia.ibama.gov.br/server/rest/services/BasesSincronizadas"

# Colunas de cada tabela de destino (além de fonte, carga_id e geom)
COLUNAS = {
    "geo.desmatamento": ["id_origem", "ano", "data_imagem", "classe", "area_km2", "satelite"],
    "geo.embargos": ["id_origem", "numero_tad", "serie_tad", "data_embargo", "municipio", "uf", "nome_imovel",
                     "infracao", "situacao_desmatamento", "area_embargada_ha"],
    "geo.unidades_conservacao": ["id_origem", "nome", "categoria", "grupo", "esfera"],
    "geo.terras_indigenas": ["id_origem", "nome", "etnia", "fase"],
    "geo.alertas_desmatamento": ["id_origem", "data_deteccao", "data_publicacao", "area_ha", "classe", "situacao",
                                 "satelite", "municipio", "uf"],
    "geo.municipios": ["id_origem", "nome", "uf", "area_km2"],
    "geo.territorios": ["id_origem", "tipo", "nome", "municipio", "uf", "fase", "familias"],
    "geo.imoveis": ["tipo", "codigo", "nome", "municipio", "uf", "situacao"],
}


@dataclass
class Fonte:
    id: str
    descricao: str
    tabela: str
    url: str
    ler: Callable[[tuple], Iterator[dict]]
    mapear: Callable[[dict], dict | None]


# ---------- auxiliares ----------
def data(v) -> date | None:
    """Aceita epoch em ms (ArcGIS) ou texto ISO (GeoServer)."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _reparar_acentos(s: str) -> str:
    """Algumas camadas do INCRA chegam com o 1º byte UTF-8 de letras acentuadas (0xC3) trocado por U+FFFD e o 2º
    byte solto (como surrogate ou caractere de controle C1). Em português, toda letra acentuada começa com 0xC3,
    então dá para reconstruir: "FAM" U+FFFD 0x8D "LIA" -> "FAMÍLIA"."""
    if "�" not in s:
        return s

    def trocar(m: re.Match) -> str:
        b = ord(m.group(1)) & 0xFF
        return bytes([0xC3, b]).decode("utf-8", "replace")

    return re.sub("�([\udc80-\udcbf\u0080-¿])", trocar, s)


def texto(v) -> str | None:
    if v is None:
        return None
    s = _reparar_acentos(str(v).strip())
    return s or None


def numero(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def inteiro(v) -> int | None:
    n = numero(v)
    return int(n) if n is not None else None


CATEGORIAS_PI = ("ESTACAO ECOLOGICA", "ESTAÇÃO ECOLÓGICA", "RESERVA BIOLOGICA", "RESERVA BIOLÓGICA", "PARQUE",
                 "MONUMENTO NATURAL", "REFUGIO DE VIDA SILVESTRE", "REFÚGIO DE VIDA SILVESTRE")


def grupo_uc(*pistas) -> str:
    """Deduz PI/US a partir de sigla de grupo, texto de proteção ou categoria."""
    for p in pistas:
        t = (texto(p) or "").upper()
        if not t:
            continue
        if t in ("PI", "US"):
            return t
        if "INTEGRAL" in t or any(t.startswith(c) for c in CATEGORIAS_PI):
            return "PI"
        if "SUSTENT" in t:
            return "US"
    return "US"


# ---------- mapeadores ----------
def m_prodes(p: dict) -> dict | None:
    if texto(p.get("main_class")) and texto(p.get("main_class")).upper() != "DESMATAMENTO":
        return None
    return {
        "id_origem": texto(p.get("uuid")) or texto(p.get("fid")),
        "ano": inteiro(p.get("year")),
        "data_imagem": data(p.get("image_date")),
        "classe": texto(p.get("class_name")),
        "area_km2": numero(p.get("area_km")),
        "satelite": texto(p.get("satellite")),
    }


def m_prodes_2000(p: dict) -> dict | None:
    d = m_prodes(p)
    if d is not None and d["ano"] is None:
        d["ano"] = 2000
    return d


def m_uc_inpe(p: dict) -> dict:
    return {"id_origem": texto(p.get("id")), "nome": texto(p.get("nome")), "categoria": texto(p.get("categoria")),
            "grupo": grupo_uc(p.get("grupo"), p.get("categoria")), "esfera": texto(p.get("esfera"))}


def m_uc_federal(p: dict) -> dict:
    return {"id_origem": texto(p.get("codigocnuc")), "nome": texto(p.get("nome")), "categoria": texto(p.get("sigla")),
            "grupo": grupo_uc(p.get("siglagrupo"), p.get("nome")), "esfera": "federal"}


def m_uc_estadual(p: dict) -> dict:
    return {"id_origem": texto(p.get("codigo")) or texto(p.get("objectid")), "nome": texto(p.get("nome")),
            "categoria": texto(p.get("categoria")),
            "grupo": grupo_uc(p.get("protecao"), p.get("tipo"), p.get("categoria"), p.get("nome")),
            "esfera": "estadual"}


def m_ti(p: dict) -> dict:
    return {"id_origem": texto(p.get("terrai_cod")), "nome": texto(p.get("terrai_nom")),
            "etnia": texto(p.get("etnia_nome")), "fase": texto(p.get("fase_ti"))}


def m_embargo(p: dict) -> dict:
    # Nome e CPF/CNPJ do embargado não são lidos nem armazenados (LGPD: minimização).
    return {
        "id_origem": texto(p.get("seq_tad")) or texto(p.get("objectid")),
        "numero_tad": texto(p.get("num_tad")),
        "serie_tad": texto(p.get("serie_tad")),
        "data_embargo": data(p.get("dat_embargo")),
        "municipio": texto(p.get("municipio")),
        "uf": texto(p.get("uf")),
        "nome_imovel": texto(p.get("nome_imovel")),
        "infracao": texto(p.get("des_infracao")),
        "situacao_desmatamento": texto(p.get("sit_desmatamento")),
        "area_embargada_ha": numero(p.get("qtd_area_embargada")),
    }


def m_deter(p: dict) -> dict:
    km2 = numero(p.get("areatotalkm"))
    return {"id_origem": texto(p.get("gid")), "data_deteccao": data(p.get("view_date")),
            "data_publicacao": data(p.get("created_date")) or data(p.get("publish_month")),
            "area_ha": km2 * 100 if km2 is not None else None, "classe": texto(p.get("classname")),
            "situacao": None, "satelite": " ".join(filter(None, [texto(p.get("satellite")), texto(p.get("sensor"))])) or None,
            "municipio": texto(p.get("municipality")), "uf": texto(p.get("uf"))}


def m_mapbiomas(p: dict) -> dict:
    classes = p.get("deforestationClasses")
    return {"id_origem": texto(p.get("alertCode")), "data_deteccao": data(p.get("detectedAt")),
            "data_publicacao": data(p.get("publishedAt")), "area_ha": numero(p.get("areaHa")),
            "classe": ", ".join(classes) if isinstance(classes, list) else texto(classes),
            "situacao": texto(p.get("statusName")),
            "satelite": ", ".join(p["sources"]) if isinstance(p.get("sources"), list) else texto(p.get("sources")),
            "municipio": None, "uf": None}


def m_municipio(p: dict) -> dict:
    return {"id_origem": texto(p.get("cd_mun")), "nome": texto(p.get("nm_mun")), "uf": texto(p.get("sigla")),
            "area_km2": numero(p.get("area_km2"))}


def m_quilombo(p: dict) -> dict:
    return {"id_origem": texto(p.get("cd_quilomb")) or texto(p.get("objectid")), "tipo": "quilombola",
            "nome": texto(p.get("nm_comunid")), "municipio": texto(p.get("nm_municip")), "uf": texto(p.get("cd_uf")),
            "fase": texto(p.get("fase")), "familias": inteiro(p.get("nr_familia"))}


def m_assentamento(p: dict) -> dict:
    return {"id_origem": texto(p.get("cd_sipra")) or texto(p.get("objectid")), "tipo": "assentamento",
            "nome": texto(p.get("nome_proje")), "municipio": texto(p.get("municipio")), "uf": texto(p.get("uf")),
            "fase": texto(p.get("descricao_")) or texto(p.get("fase")), "familias": inteiro(p.get("num_famili"))}


# Municípios da região padrão (fonte: servicodados.ibge.gov.br/api/v1/localidades). Fora da lista: "IBGE <código>".
MUNICIPIOS_IBGE = {
    "2919553": "Luís Eduardo Magalhães", "2903201": "Barreiras", "2928901": "São Desidério",
    "2926202": "Riachão das Neves", "2911105": "Formosa do Rio Preto", "2928406": "Santa Rita de Cássia",
    "1717800": "Ponte Alta do Bom Jesus", "1720903": "Taguatinga", "1707009": "Dianópolis", "1715259": "Novo Jardim",
    "1702406": "Arraias", "1702703": "Aurora do Tocantins", "1718659": "Rio da Conceição", "1705557": "Combinado",
    "1715150": "Novo Alegre", "1712157": "Lavandeira", "5204904": "Campos Belos",
}
UFS_IBGE = {"29": "BA", "17": "TO", "22": "PI", "52": "GO", "31": "MG"}


def m_sigef(p: dict) -> dict:
    cod_mun = texto(inteiro(p.get("municipio_")))
    return {"tipo": "SIGEF", "codigo": texto(p.get("parcela_co")), "nome": texto(p.get("nome_area")),
            "municipio": MUNICIPIOS_IBGE.get(cod_mun or "", f"IBGE {cod_mun}" if cod_mun else None),
            "uf": UFS_IBGE.get(texto(inteiro(p.get("uf_id"))) or "", texto(p.get("uf_id"))),
            "situacao": texto(p.get("status"))}


# ---------- catálogo ----------
def _wfs(tipo, ordenar):
    return lambda bbox: clientes.wfs(TERRABRASILIS, tipo, bbox, ordenar)


def _credenciais_mapbiomas() -> tuple[str, str]:
    """Conta gratuita em plataforma.alerta.mapbiomas.org, configurada no .env."""
    email, senha = os.getenv("MAPBIOMAS_EMAIL", ""), os.getenv("MAPBIOMAS_SENHA", "")
    if not email or not senha:
        raise RuntimeError("MapBiomas Alerta não configurado: defina MAPBIOMAS_EMAIL e MAPBIOMAS_SENHA no .env.")
    return email, senha


def mapbiomas_configurado() -> bool:
    return bool(os.getenv("MAPBIOMAS_EMAIL") and os.getenv("MAPBIOMAS_SENHA"))


def _arcgis(caminho, campos, raiz=IBAMA):
    return lambda bbox: clientes.arcgis(f"{raiz}/{caminho}", bbox, campos)


FONTES: dict[str, Fonte] = {f.id: f for f in [
    Fonte("prodes_cerrado", "PRODES Cerrado: desmatamento anual (INPE)", "geo.desmatamento",
          f"{TERRABRASILIS} · prodes-cerrado-nb:yearly_deforestation",
          _wfs("prodes-cerrado-nb:yearly_deforestation", "fid"), m_prodes),
    Fonte("prodes_cerrado_2000", "PRODES Cerrado: desmatamento acumulado até 2000 (INPE)", "geo.desmatamento",
          f"{TERRABRASILIS} · prodes-cerrado-nb:accumulated_deforestation_2000",
          _wfs("prodes-cerrado-nb:accumulated_deforestation_2000", "fid"), m_prodes_2000),
    Fonte("embargos_ibama", "Áreas embargadas (IBAMA)", "geo.embargos",
          f"{IBAMA}/adm_embargos_ibama_a/FeatureServer/0",
          _arcgis("adm_embargos_ibama_a/FeatureServer/0",
                  ["objectid", "seq_tad", "num_tad", "serie_tad", "dat_embargo", "municipio", "uf", "nome_imovel",
                   "des_infracao", "sit_desmatamento", "qtd_area_embargada"]), m_embargo),
    Fonte("ucs_cerrado_inpe", "Unidades de Conservação no Cerrado (INPE/TerraBrasilis)", "geo.unidades_conservacao",
          f"{TERRABRASILIS} · prodes-cerrado-nb:conservation_units_cerrado_biome",
          _wfs("prodes-cerrado-nb:conservation_units_cerrado_biome", "id"), m_uc_inpe),
    Fonte("ucs_federais_icmbio", "Unidades de Conservação federais (ICMBio)", "geo.unidades_conservacao",
          f"{IBAMA}/lim_unidade_conservacao_a/FeatureServer/13",
          _arcgis("lim_unidade_conservacao_a/FeatureServer/13", ["objectid", "codigocnuc", "nome", "sigla", "siglagrupo"]),
          m_uc_federal),
    Fonte("ucs_estaduais_sfb", "Unidades de Conservação estaduais (SFB)", "geo.unidades_conservacao",
          f"{IBAMA}/lim_unidade_conservacao_est_a/FeatureServer/14",
          _arcgis("lim_unidade_conservacao_est_a/FeatureServer/14",
                  ["objectid_1", "objectid", "codigo", "nome", "categoria", "protecao", "tipo"]), m_uc_estadual),
    Fonte("tis_funai", "Terras Indígenas (FUNAI)", "geo.terras_indigenas",
          f"{IBAMA}/lim_terra_indigena_a/FeatureServer/12",
          _arcgis("lim_terra_indigena_a/FeatureServer/12", ["objectid", "terrai_cod", "terrai_nom", "etnia_nome", "fase_ti"]),
          m_ti),
    Fonte("deter_cerrado", "DETER Cerrado: alertas de desmatamento (INPE)", "geo.alertas_desmatamento",
          f"{TERRABRASILIS} · deter-cerrado-nb:deter_cerrado",
          _wfs("deter-cerrado-nb:deter_cerrado", "gid"), m_deter),
    Fonte("mapbiomas_alerta", "MapBiomas Alerta: alertas validados de desmatamento", "geo.alertas_desmatamento",
          "https://plataforma.alerta.mapbiomas.org",
          lambda bbox: clientes.mapbiomas_alertas(*_credenciais_mapbiomas(), bbox), m_mapbiomas),
    Fonte("municipios_ibge", "Limites municipais (IBGE 2021)", "geo.municipios",
          f"{IBAMA}/lim_mun_ibge_250k_2021/FeatureServer/11",
          _arcgis("lim_mun_ibge_250k_2021/FeatureServer/11", ["objectid", "cd_mun", "nm_mun", "sigla", "area_km2"]),
          m_municipio),
    Fonte("quilombolas_incra", "Territórios quilombolas (INCRA)", "geo.territorios",
          f"{IBAMA_SINC}/lim_quilombos_incra_a/FeatureServer/0",
          _arcgis("lim_quilombos_incra_a/FeatureServer/0",
                  ["objectid", "cd_quilomb", "nm_comunid", "nm_municip", "cd_uf", "fase", "nr_familia"], IBAMA_SINC),
          m_quilombo),
    Fonte("assentamentos_incra", "Assentamentos da reforma agrária (INCRA)", "geo.territorios",
          f"{IBAMA}/assentamentos_incra/FeatureServer/1",
          _arcgis("assentamentos_incra/FeatureServer/1",
                  ["objectid", "cd_sipra", "nome_proje", "municipio", "uf", "fase", "descricao_", "num_famili"]),
          m_assentamento),
    Fonte("sigef_privado", "Imóveis certificados SIGEF privados (INCRA)", "geo.imoveis",
          f"{IBAMA}/lim_imovel_sigef_privado_a/FeatureServer/9",
          _arcgis("lim_imovel_sigef_privado_a/FeatureServer/9",
                  ["objectid", "parcela_co", "nome_area", "status", "municipio_", "uf_id"]), m_sigef),
]}
