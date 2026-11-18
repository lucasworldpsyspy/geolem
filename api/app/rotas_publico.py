"""Rotas públicas do observatório: números da região, consulta grátis, relatório do município e boletim do DETER.

Toda captura de contato exige o consentimento explícito, que fica gravado com data, texto e IP (LGPD).
"""
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from . import contas, observatorio, regras
from .analise import ErroAnalise, analisar
from .db import pool
from .dependencias import ip as ip_de
from .imoveis import imovel_por_codigo

rotas = APIRouter(prefix="/api/publico", tags=["público"])

CONSENTIMENTO = ("Autorizo a GeoLEM a guardar meus dados de contato para enviar o que pedi e para falar comigo sobre "
                 "o serviço. Posso pedir a exclusão a qualquer momento. (Política de Privacidade v1.0)")
PERFIS = {"armazem", "revenda", "trading", "cooperativa", "banco", "consultoria", "agroindustria", "produtor", "outro"}

# Contra raspagem: a consulta grátis existe para gerar contato, não para substituir o laudo
limite_consulta_ip = contas.LimiteTentativas(5, 24 * 3600)
limite_consulta_email = contas.LimiteTentativas(3, 24 * 3600)
limite_formulario_ip = contas.LimiteTentativas(20, 3600)


class Contato(BaseModel):
    nome: str = Field(..., min_length=2, max_length=120)
    email: str = Field(..., max_length=200)
    telefone: str | None = Field(None, max_length=40)
    empresa: str | None = Field(None, max_length=160)
    perfil: str | None = Field(None, max_length=30)
    consentimento: bool = False


def _validar(c: Contato) -> str:
    if not c.consentimento:
        raise HTTPException(422, "Para continuar, marque a autorização de uso dos seus dados.")
    try:
        email = contas.normalizar_email(c.email)
    except contas.ErroConta as e:
        raise HTTPException(422, str(e)) from e
    if c.perfil and c.perfil not in PERFIS:
        raise HTTPException(422, "Perfil inválido.")
    return email


def _gravar_lead(conn, c: Contato, email: str, interesse: str, detalhe: dict, ip: str | None,
                 municipio: str | None = None, boletim: bool = False) -> int:
    prospect = conn.execute("SELECT id FROM app.prospects WHERE lower(email) = %s ORDER BY id LIMIT 1",
                            (email,)).fetchone()
    lid = conn.execute(
        """INSERT INTO app.leads (nome, email, telefone, empresa, perfil, municipio, interesse, detalhe,
                                  consentimento_em, consentimento_txt, ip, boletim, boletim_municipio, token_saida,
                                  prospect_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s) RETURNING id""",
        (c.nome.strip(), email, (c.telefone or "").strip() or None, (c.empresa or "").strip() or None, c.perfil,
         municipio, interesse, Jsonb(detalhe), CONSENTIMENTO, ip, boletim, municipio if boletim else None,
         secrets.token_urlsafe(18) if boletim else None, prospect["id"] if prospect else None),
    ).fetchone()["id"]
    if prospect:
        conn.execute("INSERT INTO app.interacoes (prospect_id, tipo, texto) VALUES (%s, 'nota', %s)",
                     (prospect["id"], f"Novo contato pelo site: {interesse}"))
    return lid


@rotas.get("/observatorio")
def numeros_da_regiao():
    with pool.connection() as conn:
        return observatorio.dados(conn)


@rotas.get("/consentimento")
def texto_consentimento():
    return {"texto": CONSENTIMENTO}


class PedidoConsulta(Contato):
    codigo: str = Field(..., min_length=6, max_length=80)


@rotas.post("/consulta")
def consulta_gratis(p: PedidoConsulta, request: Request):
    """Situação resumida de um imóvel pelo código SIGEF/CAR: resultado geral e quais verificações têm ocorrência."""
    email = _validar(p)
    ip = ip_de(request) or "-"
    if limite_consulta_ip.bloqueado(ip) or limite_consulta_email.bloqueado(email):
        raise HTTPException(429, "Limite de consultas grátis atingido hoje. Fale com a gente para conhecer o plano completo.")
    with pool.connection() as conn:
        im = imovel_por_codigo(conn, p.codigo)
        if not im:
            raise HTTPException(404, "Código não encontrado nas bases (SIGEF/CAR) da região coberta.")
        im.pop("geom_json", None)
        geom = conn.execute("SELECT ST_AsGeoJSON(geom, 6) AS g FROM geo.imoveis WHERE id = %s", (im["id"],)).fetchone()
        import json
        try:
            r = analisar(conn, json.loads(geom["g"]), imovel=im, gravar=False, com_imagens=False)
        except ErroAnalise as e:
            raise HTTPException(422, str(e)) from e
        limite_consulta_ip.falhou(ip)
        limite_consulta_email.falhou(email)
        ocorrencias = [{"titulo": c["titulo"], "status": c["status"], "rotulo": regras.ROTULOS[c["status"]]}
                       for c in r["checks"] if c["status"] in ("bad", "warn") and regras.entra_no_resultado(c)]
        _gravar_lead(conn, p, email, "consulta", {"codigo": p.codigo, "imovel": im["nome"], "resultado": r["geral"]},
                     ip, municipio=im["municipio"])
    return {"imovel": {"nome": im["nome"], "municipio": im["municipio"], "uf": im["uf"],
                       "area_ha": round(im["area_ha"] or 0, 1)},
            "geral": r["geral"], "rotulo": r["rotulo_geral"], "resumo": r["resumo"],
            "verificacoes": len([c for c in r["checks"] if regras.entra_no_resultado(c)]), "ocorrencias": ocorrencias,
            "renovabio": next(({"rotulo": c["rotulo"], "status": c["status"]} for c in r["checks"]
                               if c["id"] == "renovabio"), None)}


class PedidoRelatorio(Contato):
    municipio: str = Field(..., max_length=100)
    boletim: bool = False


@rotas.post("/relatorio")
def relatorio_municipio(p: PedidoRelatorio, request: Request):
    email = _validar(p)
    ip = ip_de(request) or "-"
    if limite_formulario_ip.bloqueado(ip):
        raise HTTPException(429, "Muitos pedidos. Tente de novo em uma hora.")
    limite_formulario_ip.falhou(ip)
    with pool.connection() as conn:
        pdf = observatorio.pdf_municipio(conn, p.municipio)
        if not pdf:
            raise HTTPException(404, "Município fora da região coberta.")
        _gravar_lead(conn, p, email, "relatorio", {"municipio": p.municipio}, ip, municipio=p.municipio,
                     boletim=p.boletim)
    nome = observatorio.chave_municipio(p.municipio).lower().replace(" ", "-")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="geolem-{nome}.pdf"'})


class PedidoBoletim(Contato):
    municipio: str = Field(..., max_length=100)


@rotas.post("/boletim")
def assinar_boletim(p: PedidoBoletim, request: Request):
    email = _validar(p)
    ip = ip_de(request) or "-"
    if limite_formulario_ip.bloqueado(ip):
        raise HTTPException(429, "Muitos pedidos. Tente de novo em uma hora.")
    limite_formulario_ip.falhou(ip)
    with pool.connection() as conn:
        if not observatorio.municipio(conn, p.municipio):
            raise HTTPException(404, "Município fora da região coberta.")
        conn.execute("UPDATE app.leads SET boletim = false WHERE lower(email) = %s AND boletim_municipio = %s",
                     (email, p.municipio))
        _gravar_lead(conn, p, email, "boletim", {"municipio": p.municipio}, ip, municipio=p.municipio, boletim=True)
    return {"ok": True}


@rotas.get("/sair", response_class=HTMLResponse)
def sair_do_boletim(token: str):
    with pool.connection() as conn:
        n = conn.execute("UPDATE app.leads SET boletim = false WHERE token_saida = %s AND boletim", (token,)).rowcount
    msg = "Pronto: você não vai mais receber o boletim." if n else "Este link já foi usado ou não é válido."
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<link rel="stylesheet" href="/estilo.css"><div class="documento"><h1>Boletim do DETER</h1><p>{msg}</p>'
            f'<p><a href="/observatorio.html">Voltar ao observatório</a></p></div>')
