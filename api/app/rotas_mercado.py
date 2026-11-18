"""Rotas do mercado de lotes de origem verificada (usuários logados de qualquer empresa cliente)."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import avisos, contas, lotes
from .db import pool
from .dependencias import ip as ip_de
from .dependencias import usuario

rotas = APIRouter(prefix="/api/mercado", tags=["mercado"])


@rotas.get("/lotes")
def lotes_disponiveis(cultura: str | None = Query(None, max_length=20), municipio: str | None = Query(None, max_length=80),
                      so_conformes: bool = False, renovabio: bool = False, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        return lotes.listar(conn, u["empresa_id"], cultura or None, municipio or None, so_conformes, renovabio)


class NovoLote(BaseModel):
    area_id: int
    safra: str = Field(..., min_length=4, max_length=20)
    volume_t: float = Field(..., gt=0, le=1_000_000)
    preco_pedido: float | None = Field(None, gt=0, le=1_000_000)
    entrega: str | None = Field(None, max_length=120)
    disponivel_ate: date | None = None
    observacoes: str | None = Field(None, max_length=1000)


@rotas.post("/lotes")
def anunciar(p: NovoLote, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        try:
            lid = lotes.anunciar(conn, u["empresa_id"], u["id"], **p.model_dump())
        except lotes.ErroMercado as e:
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, "lote_anunciado", u, alvo=str(lid), detalhes={"volume_t": p.volume_t}, ip=ip_de(request))
    return {"id": lid}


class AlteracaoLote(BaseModel):
    situacao: str


@rotas.patch("/lotes/{lote_id}")
def alterar(lote_id: int, p: AlteracaoLote, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        try:
            lotes.alterar_lote(conn, u["empresa_id"], lote_id, p.situacao)
        except lotes.ErroMercado as e:
            raise HTTPException(422, str(e)) from e
    return {"ok": True}


class Interesse(BaseModel):
    volume_t: float | None = Field(None, gt=0, le=1_000_000)
    mensagem: str | None = Field(None, max_length=1000)


@rotas.post("/lotes/{lote_id}/interesse")
def manifestar(lote_id: int, p: Interesse, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        try:
            iid = lotes.manifestar(conn, u["empresa_id"], u["id"], lote_id, p.volume_t, p.mensagem)
        except lotes.ErroMercado as e:
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, "interesse_lote", u, alvo=str(lote_id), ip=ip_de(request))
        try:
            avisos.aviso_interesse(conn, lote_id)
        except Exception as e:  # e-mail é bônus: falha não desfaz o interesse
            print(f"Aviso de interesse não enviado: {e}", flush=True)
    return {"id": iid}


@rotas.get("/meus")
def meus(u: dict = Depends(usuario)):
    with pool.connection() as conn:
        return lotes.meus(conn, u["empresa_id"])


class Resposta(BaseModel):
    acao: str  # aceitar | recusar | fechar
    volume_t: float | None = Field(None, gt=0, le=1_000_000)
    preco: float | None = Field(None, gt=0, le=1_000_000)


@rotas.post("/interesses/{interesse_id}/responder")
def responder(interesse_id: int, p: Resposta, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        try:
            lotes.responder(conn, u["empresa_id"], interesse_id, p.acao, p.volume_t, p.preco)
        except lotes.ErroMercado as e:
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, f"interesse_{p.acao}", u, alvo=str(interesse_id), ip=ip_de(request))
    return {"ok": True}
