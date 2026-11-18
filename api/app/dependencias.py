"""Quem está chamando: usuário logado, administrador da empresa, administrador da plataforma e IP."""
from fastapi import Depends, HTTPException, Request


def usuario(request: Request) -> dict:
    u = request.state.usuario
    if not u:
        raise HTTPException(401, "Faça login para continuar.")
    return u


def admin(u: dict = Depends(usuario)) -> dict:
    if u["papel"] != "admin":
        raise HTTPException(403, "Só administradores da empresa podem fazer isso.")
    return u


def superadmin(u: dict = Depends(usuario)) -> dict:
    if not u.get("superadmin"):
        raise HTTPException(403, "Área restrita à administração da plataforma.")
    return u


def ip(request: Request) -> str | None:
    # Atrás do Caddy, o IP real vem no X-Forwarded-For (o Caddy sobrescreve o que o cliente mandar)
    return (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (
        request.client.host if request.client else None)
