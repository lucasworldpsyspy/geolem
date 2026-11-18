"""Regras de conformidade socioambiental do GeoLEM.

Funções puras, sem banco, para poderem ser testadas e auditadas isoladamente.
"""
from datetime import date

MARCO_CODIGO_FLORESTAL = date(2008, 7, 22)  # Lei 12.651/2012, art. 3º, IV
CORTE_EUDR = date(2020, 12, 31)             # Regulamento (UE) 2023/1115, art. 2º, 13
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
