"""Regras de conformidade socioambiental do GeoLEM.

Funções puras, sem banco, para poderem ser testadas e auditadas isoladamente.
"""
from datetime import date

MARCO_CODIGO_FLORESTAL = date(2008, 7, 22)  # Lei 12.651/2012, art. 3º, IV
CORTE_EUDR = date(2020, 12, 31)             # Regulamento (UE) 2023/1115, art. 2º, 13
# RenovaBio: biomassa elegível só de área sem supressão de vegetação nativa desde a vigência da Resolução ANP
# 758/2018 (critério mantido pela Resolução ANP 984/2025) e com CAR ativo ou pendente.
CORTE_RENOVABIO = date(2018, 11, 27)
RESERVA_LEGAL_MINIMA = 0.20                 # Cerrado fora da Amazônia Legal, art. 12, II

ORDEM = {"ok": 0, "info": 1, "warn": 2, "bad": 3}
ROTULOS = {"ok": "Conforme", "info": "Informativo", "warn": "Atenção", "bad": "Não conforme"}
RESUMOS = {
    "ok": "Nenhum impedimento socioambiental encontrado nas bases consultadas.",
    "warn": "Há pontos de atenção que pedem análise técnica.",
    "bad": "Há impedimentos para comercialização ou crédito.",
}


def classificar_desmatamento(ano: int | None, data_imagem: date | None) -> str:
    """Classifica um polígono de supressão em: pos_2020, pos_2008 ou consolidado.

    O ano PRODES vai de agosto a julho, então a data da imagem é mais precisa que o ano.
    Sem data nem ano, assume o caso mais conservador que ainda exige análise (pos_2008).
    """
    if data_imagem is not None:
        if data_imagem > CORTE_EUDR:
            return "pos_2020"
        if data_imagem > MARCO_CODIGO_FLORESTAL:
            return "pos_2008"
        return "consolidado"
    if ano is None:
        return "pos_2008"
    if ano >= 2021:
        return "pos_2020"
    if ano >= 2009:
        return "pos_2008"
    return "consolidado"


def status_uc(grupo: str | None) -> str:
    return "bad" if grupo == "PI" else "warn"


# Alertas de desmatamento: MapBiomas Alerta é validado por analistas com imagem de alta resolução;
# o DETER é um alerta rápido, que ainda precisa ser confirmado pelo PRODES ou por imagem.
STATUS_ALERTA = {"mapbiomas_alerta": "bad", "deter_cerrado": "warn"}


def alerta_pos_corte(data_deteccao: date | None) -> bool:
    """Só interessam alertas posteriores ao corte do EUDR. Sem data, considera (caso conservador)."""
    return data_deteccao is None or data_deteccao > CORTE_EUDR


def supressao_pos_renovabio(ano: int | None, data: date | None) -> bool:
    """Supressão posterior ao corte do RenovaBio. Sem data, usa o ano PRODES (agosto a julho): o PRODES 2019
    (ago/2018 a jul/2019) já inclui meses depois do corte, então conta (caso conservador)."""
    if data is not None:
        return data > CORTE_RENOVABIO
    return ano is None or ano >= 2019


SITUACOES_CAR_ELEGIVEIS = ("ATIVO", "PENDENTE")


def avaliar_renovabio(ha_supressao: float, situacao_car: str | None, tipo_imovel: str | None,
                      area_minima_ha: float) -> tuple[str, str, str]:
    """(status, rótulo, texto). Fora do resultado geral: é um critério do programa, não de conformidade."""
    if ha_supressao >= area_minima_ha:
        return ("bad", "Inelegível",
                f"Supressão de vegetação nativa após {CORTE_RENOVABIO:%d/%m/%Y}: {fmt_ha(ha_supressao)}. "
                "A biomassa desta área não pode gerar CBIOs, salvo supressão legalmente autorizada antes do corte.")
    if tipo_imovel == "CAR" and situacao_car:
        if any(s in situacao_car.upper() for s in SITUACOES_CAR_ELEGIVEIS):
            return "ok", "Elegível", f"Sem supressão após o corte e CAR {situacao_car.lower()}."
        return "bad", "Inelegível", f"CAR com situação '{situacao_car}': o programa exige CAR ativo ou pendente."
    return ("ok", "Elegível (uso do solo)",
            f"Sem supressão de vegetação nativa após {CORTE_RENOVABIO:%d/%m/%Y}. "
            "Confirme no SICAR se o CAR do imóvel está ativo ou pendente.")


# Fases em que o território quilombola já foi reconhecido formalmente (portaria, decreto ou título)
FASES_QUILOMBO_RECONHECIDO = ("TITUL", "DECRETO", "PORTARIA")


def status_quilombo(fase: str | None) -> str:
    """Território reconhecido: Não conforme. Em estudo (RTID, abertura de processo): Atenção."""
    f = (fase or "").upper()
    return "bad" if any(p in f for p in FASES_QUILOMBO_RECONHECIDO) else "warn"


def avaliar_reserva_legal(reserva_ha: float | None, area_ha: float) -> tuple[str, str]:
    if reserva_ha is None:
        return "info", "Não informada. Informe a reserva legal declarada no CAR para verificar."
    if area_ha <= 0:
        return "info", "Área do imóvel inválida para o cálculo."
    pct = reserva_ha / area_ha
    texto = f"Declarada: {fmt_ha(reserva_ha)} ({fmt_pct(pct)} do imóvel)"
    if pct >= RESERVA_LEGAL_MINIMA:
        return "ok", texto
    deficit = area_ha * RESERVA_LEGAL_MINIMA - reserva_ha
    return "warn", f"{texto}. Déficit: {fmt_ha(deficit)}"


def entra_no_resultado(check: dict) -> bool:
    """Imóveis sobrepostos e elegibilidade RenovaBio aparecem no laudo, mas não mudam o resultado geral."""
    return check["id"] not in ("imoveis", "renovabio")


def status_geral(status: list[str]) -> str:
    pior = max(status, key=lambda s: ORDEM[s], default="ok")
    return "ok" if pior == "info" else pior


def fmt_ha(v: float) -> str:
    texto = f"{v:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if texto.endswith(",0"):
        texto = texto[:-2]
    return f"{texto} ha"


def fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}".replace(".", ",") + "%"
