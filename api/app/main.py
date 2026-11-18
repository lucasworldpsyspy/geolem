import csv
import io
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import carteira as cart
from . import contas, convites, licencas, mercado, painel, tiles
from .dependencias import admin, superadmin, usuario
from .dependencias import ip as _ip
from .rotas_admin import rotas as rotas_admin
from .rotas_publico import rotas as rotas_publico
from .analise import ErroAnalise, analisar
from .arquivos import ErroArquivo, ler_feicoes, ler_geometria
from .config import COOKIE_SEGURO, REGIAO_BBOX, TAMANHO_MAX_ARQUIVO, URL_PUBLICA, WEB_DIR
from .db import pool
from .eudr import geojson_eudr
from .imoveis import COLUNAS_IMOVEL, carregar_imovel
from .laudo_pdf import codigo_integridade, gerar_pdf
from .planilha import ErroPlanilha, ler_planilha


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(title="GeoLEM API", version="0.3.0", lifespan=ciclo_de_vida,
              description="Compliance socioambiental de áreas rurais no oeste da Bahia.")

COOKIE = "geolem_sessao"
app.include_router(rotas_publico)
app.include_router(rotas_admin)

# ---------- Acesso ----------
# Sem login: a tela de entrada, os termos, o observatório (e suas rotas), a saúde do serviço e a verificação
# pública de laudos (QR code).
PUBLICO_EXATO = {"/entrar.html", "/termos.html", "/privacidade.html", "/observatorio.html", "/convite.html", "/api/auth/login",
                 "/api/saude", "/favicon.ico", "/marca.svg", "/estilo.css"}
PUBLICO_PREFIXO = ("/api/publico/",)
PUBLICO_LEITURA = re.compile(r"^/(verificar/[^/]+|api/analises/GL-[^/]+(/laudo\.pdf|/imagens/(antes|depois)\.png)?)$")
TROCA_DE_SENHA = {"/api/auth/eu", "/api/auth/senha", "/api/auth/sair"}


def _publico(request: Request) -> bool:
    p = request.url.path
    return (p in PUBLICO_EXATO or p.startswith(PUBLICO_PREFIXO)
            or (request.method in ("GET", "HEAD") and bool(PUBLICO_LEITURA.match(p))))


def _origem_confiavel(request: Request) -> bool:
    """Proteção contra CSRF: escrita só a partir de páginas do próprio sistema (além do cookie SameSite=Lax)."""
    origem = request.headers.get("origin") or request.headers.get("referer")
    if not origem:
        return True  # chamadas de linha de comando e integrações (sem navegador)
    return urlparse(origem).netloc == request.headers.get("host")


def _sessao(token: str | None) -> dict | None:
    with pool.connection() as conn:
        return contas.usuario_da_sessao(conn, token)


@app.middleware("http")
async def proteger(request: Request, chamar):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _origem_confiavel(request):
        return JSONResponse({"detail": "Origem não autorizada."}, status_code=403)
    request.state.usuario = None
    if _publico(request):
        resposta = await chamar(request)
    else:
        usuario = await run_in_threadpool(_sessao, request.cookies.get(COOKIE))
        api = request.url.path.startswith("/api/") or request.url.path in ("/docs", "/openapi.json")
        if not usuario:
            if api:
                return JSONResponse({"detail": "Faça login para continuar."}, status_code=401)
            return RedirectResponse(f"/entrar.html?volta={quote(request.url.path)}", status_code=303)
        if usuario["trocar_senha"] and request.url.path not in TROCA_DE_SENHA:
            if api:
                return JSONResponse({"detail": "Troque a senha provisória antes de continuar."}, status_code=403)
            return RedirectResponse("/entrar.html?trocar=1", status_code=303)
        bloqueio = licencas.motivo_bloqueio(usuario)
        if bloqueio and request.url.path not in TROCA_DE_SENHA:
            if api:
                return JSONResponse({"detail": bloqueio}, status_code=403)
            return RedirectResponse("/entrar.html?bloqueado=1", status_code=303)
        if request.url.path == "/admin.html" and not usuario["superadmin"]:
            return RedirectResponse("/", status_code=303)
        request.state.usuario = usuario
        resposta = await chamar(request)
    resposta.headers.setdefault("X-Frame-Options", "DENY")
    return resposta


def _publico_usuario(u: dict) -> dict:
    return {"id": u["id"], "nome": u["nome"], "email": u["email"], "papel": u["papel"],
            "trocar_senha": u["trocar_senha"], "superadmin": bool(u.get("superadmin")),
            "empresa": {"id": u["empresa_id"], "nome": u["empresa_nome"], "plano": u.get("plano"),
                        "situacao": u.get("situacao"),
                        "valido_ate": u["valido_ate"].isoformat() if u.get("valido_ate") else None}}


# ---------- Autenticação ----------
class PedidoLogin(BaseModel):
    email: str = Field(..., max_length=200)
    senha: str = Field(..., max_length=200)


@app.post("/api/auth/login")
def login(p: PedidoLogin, request: Request):
    with pool.connection() as conn:
        try:
            token, u = contas.entrar(conn, p.email, p.senha, _ip(request), request.headers.get("user-agent"))
        except contas.ErroConta as e:
            conn.commit()  # grava a tentativa que falhou na auditoria
            raise HTTPException(401, str(e)) from e
    resposta = JSONResponse(_publico_usuario(u))
    resposta.set_cookie(COOKIE, token, httponly=True, secure=COOKIE_SEGURO, samesite="lax",
                        max_age=int(contas.DURACAO_MAXIMA.total_seconds()), path="/")
    return resposta


@app.post("/api/auth/sair")
def sair(request: Request):
    with pool.connection() as conn:
        contas.sair(conn, request.cookies.get(COOKIE))
        if request.state.usuario:
            contas.auditar(conn, "logout", request.state.usuario, ip=_ip(request))
    resposta = JSONResponse({"ok": True})
    resposta.delete_cookie(COOKIE, path="/")
    return resposta


@app.get("/api/auth/eu")
def eu(u: dict = Depends(usuario)):
    return _publico_usuario(u)


class PedidoSenha(BaseModel):
    atual: str = Field(..., max_length=200)
    nova: str = Field(..., max_length=200)


@app.post("/api/auth/senha")
def mudar_senha(p: PedidoSenha, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        try:
            contas.trocar_senha(conn, u, p.atual, p.nova, request.cookies.get(COOKIE))
        except contas.ErroConta as e:
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, "senha_trocada", u, ip=_ip(request))
    return {"ok": True}


# ---------- Equipe (administradores da empresa) ----------
@app.get("/api/equipe")
def equipe(u: dict = Depends(admin)):
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT id, nome, email, papel, ativo, trocar_senha, criado_em, ultimo_acesso
               FROM app.usuarios WHERE empresa_id = %s ORDER BY nome""", (u["empresa_id"],)).fetchall()
    return [{**l, "criado_em": l["criado_em"].isoformat(),
             "ultimo_acesso": l["ultimo_acesso"].isoformat() if l["ultimo_acesso"] else None} for l in linhas]


class PedidoUsuario(BaseModel):
    nome: str = Field(..., max_length=120)
    email: str = Field(..., max_length=200)
    papel: str = "analista"


@app.post("/api/equipe")
def novo_usuario(p: PedidoUsuario, request: Request, u: dict = Depends(admin)):
    with pool.connection() as conn:
        try:
            licencas.exigir(conn, u["empresa_id"], "usuarios")
            uid, senha = contas.criar_usuario(conn, u["empresa_id"], p.nome, p.email, p.papel)
        except (contas.ErroConta, licencas.ErroLicenca) as e:
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, "usuario_criado", u, alvo=p.email, detalhes={"papel": p.papel}, ip=_ip(request))
    return {"id": uid, "senha_provisoria": senha}


class AlteracaoUsuario(BaseModel):
    ativo: bool | None = None
    papel: str | None = None


def _do_mesmo_time(conn, u: dict, usuario_id: int) -> dict:
    alvo = conn.execute("SELECT id, email FROM app.usuarios WHERE id = %s AND empresa_id = %s",
                        (usuario_id, u["empresa_id"])).fetchone()
    if not alvo:
        raise HTTPException(404, "Usuário não encontrado.")
    return alvo


@app.patch("/api/equipe/{usuario_id}")
def alterar_usuario(usuario_id: int, p: AlteracaoUsuario, request: Request, u: dict = Depends(admin)):
    if usuario_id == u["id"]:
        raise HTTPException(422, "Você não pode alterar o próprio acesso.")
    if p.papel is not None and p.papel not in ("admin", "analista"):
        raise HTTPException(422, "Papel inválido.")
    with pool.connection() as conn:
        alvo = _do_mesmo_time(conn, u, usuario_id)
        if p.ativo is not None:
            conn.execute("UPDATE app.usuarios SET ativo = %s WHERE id = %s", (p.ativo, usuario_id))
            if not p.ativo:
                conn.execute("DELETE FROM app.sessoes WHERE usuario_id = %s", (usuario_id,))
        if p.papel is not None:
            conn.execute("UPDATE app.usuarios SET papel = %s WHERE id = %s", (p.papel, usuario_id))
        contas.auditar(conn, "usuario_alterado", u, alvo=alvo["email"], detalhes=p.model_dump(exclude_none=True),
                       ip=_ip(request))
    return {"ok": True}


@app.post("/api/equipe/{usuario_id}/redefinir-senha")
def redefinir(usuario_id: int, request: Request, u: dict = Depends(admin)):
    with pool.connection() as conn:
        alvo = _do_mesmo_time(conn, u, usuario_id)
        senha = contas.redefinir_senha(conn, usuario_id)
        contas.auditar(conn, "senha_redefinida", u, alvo=alvo["email"], ip=_ip(request))
    return {"senha_provisoria": senha}


ACOES = {
    "login": "Entrou no sistema", "login_falhou": "Tentativa de login recusada", "logout": "Saiu do sistema",
    "senha_trocada": "Trocou a própria senha", "senha_redefinida": "Redefiniu a senha de", "usuario_criado": "Criou o usuário",
    "usuario_alterado": "Alterou o acesso de", "analise": "Emitiu laudo", "carteira_importada": "Importou áreas na carteira",
    "carteira_area": "Adicionou área à carteira", "carteira_removida": "Removeu área da carteira",
    "carteira_reanalise": "Pediu reanálise da carteira", "empresa_criada": "Empresa criada",
}


@app.get("/api/auditoria")
def auditoria(limite: int = Query(200, ge=1, le=1000), u: dict = Depends(admin)):
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT a.em, a.acao, a.alvo, a.detalhes, a.ip, us.nome AS usuario
               FROM app.auditoria a LEFT JOIN app.usuarios us ON us.id = a.usuario_id
               WHERE a.empresa_id = %s ORDER BY a.em DESC LIMIT %s""", (u["empresa_id"], limite)).fetchall()
    return [{**l, "em": l["em"].isoformat(), "descricao": ACOES.get(l["acao"], l["acao"])} for l in linhas]


# ---------- Saúde e bases ----------
@app.get("/api/saude")
def saude():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/bases")
def bases():
    """Última carga bem-sucedida de cada base pública e a região coberta."""
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT DISTINCT ON (fonte) fonte, tabela, descricao, url, concluido_em, registros
               FROM geo.cargas WHERE status = 'ok' ORDER BY fonte, concluido_em DESC"""
        ).fetchall()
    return {
        "regiao_bbox": REGIAO_BBOX,
        "bases": [{**l, "concluido_em": l["concluido_em"].isoformat() if l["concluido_em"] else None} for l in linhas],
    }


# ---------- Imóveis ----------
@app.get("/api/imoveis")
def buscar_imoveis(q: str = Query(..., min_length=3, max_length=100), limite: int = Query(15, ge=1, le=50)):
    """Busca por código (SIGEF/CAR) ou nome do imóvel."""
    termo = q.strip()
    with pool.connection() as conn:
        linhas = conn.execute(
            f"""SELECT {COLUNAS_IMOVEL},
                       GREATEST(similarity(unaccent(coalesce(nome, '')), unaccent(%(t)s)),
                                CASE WHEN codigo ILIKE %(p)s THEN 1 ELSE 0 END) AS relevancia
                FROM geo.imoveis
                -- sem acento dos dois lados: "sao joao" acha "SÃO JOÃO"
                WHERE codigo ILIKE %(p)s OR unaccent(nome) ILIKE unaccent(%(p)s)
                   OR unaccent(nome) %% unaccent(%(t)s)
                ORDER BY relevancia DESC, area_ha DESC NULLS LAST
                LIMIT %(l)s""",
            {"t": termo, "p": f"%{termo}%", "l": limite},
        ).fetchall()
    return linhas


@app.get("/api/imoveis/no-ponto")
def imoveis_no_ponto(lon: float = Query(..., ge=-180, le=180), lat: float = Query(..., ge=-90, le=90)):
    with pool.connection() as conn:
        return conn.execute(
            f"""SELECT {COLUNAS_IMOVEL} FROM geo.imoveis
                WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                ORDER BY area_ha ASC LIMIT 10""",
            (lon, lat),
        ).fetchall()


@app.get("/api/imoveis/{imovel_id}")
def obter_imovel(imovel_id: int):
    with pool.connection() as conn:
        im = carregar_imovel(conn, imovel_id)
    if not im:
        raise HTTPException(404, "Imóvel não encontrado.")
    geom = json.loads(im.pop("geom_json"))
    return {"type": "Feature", "properties": im, "geometry": geom}


# ---------- Análises ----------
class PedidoAnalise(BaseModel):
    geometria: dict[str, Any] | None = Field(None, description="GeoJSON (Geometry ou Feature) do polígono")
    imovel_id: int | None = None
    nome: str | None = Field(None, max_length=200)
    reserva_legal_ha: float | None = Field(None, ge=0)


@app.post("/api/analises")
def criar_analise(p: PedidoAnalise, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        imovel = None
        geometria = p.geometria
        if p.imovel_id is not None:
            im = carregar_imovel(conn, p.imovel_id)
            if not im:
                raise HTTPException(404, "Imóvel não encontrado.")
            geom_imovel = json.loads(im.pop("geom_json"))
            imovel = im
            geometria = geometria or geom_imovel
        if not geometria:
            raise HTTPException(422, "Envie a geometria da área ou o id de um imóvel.")
        if geometria.get("type") == "Feature":
            geometria = geometria.get("geometry") or {}
        if geometria.get("type") not in ("Polygon", "MultiPolygon"):
            raise HTTPException(422, "A geometria precisa ser Polygon ou MultiPolygon.")
        try:
            licencas.exigir(conn, u["empresa_id"], "laudos_mes")
        except licencas.ErroLicenca as e:
            raise HTTPException(403, str(e)) from e
        try:
            r = analisar(conn, geometria, nome=p.nome, imovel=imovel, reserva_legal_ha=p.reserva_legal_ha,
                         empresa_id=u["empresa_id"], usuario_id=u["id"])
        except ErroAnalise as e:
            conn.rollback()
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, "analise", u, alvo=r["protocolo"],
                       detalhes={"area": r["area"]["nome"], "resultado": r["geral"]}, ip=_ip(request))
        return r


@app.get("/api/analises")
def historico(limite: int = Query(100, ge=1, le=500), u: dict = Depends(usuario)):
    """Laudos emitidos pela empresa, do mais recente para o mais antigo."""
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT an.protocolo, an.criado_em, an.nome_area, round(an.area_ha::numeric, 2) AS area_ha, an.resultado,
                      us.nome AS usuario, an.area_id
               FROM geo.analises an LEFT JOIN app.usuarios us ON us.id = an.usuario_id
               WHERE an.empresa_id = %s ORDER BY an.criado_em DESC LIMIT %s""", (u["empresa_id"], limite)).fetchall()
    return [{**l, "criado_em": l["criado_em"].isoformat(), "area_ha": float(l["area_ha"])} for l in linhas]


@app.get("/api/analises/{protocolo}")
def obter_analise(protocolo: str):
    """Permite conferir a autenticidade de um laudo pelo protocolo."""
    return _analise_gravada(protocolo)


def _analise_gravada(protocolo: str) -> dict:
    with pool.connection() as conn:
        linha = conn.execute("SELECT detalhes FROM geo.analises WHERE protocolo = %s",
                             (protocolo.strip().upper(),)).fetchone()
    if not linha:
        raise HTTPException(404, "Protocolo não encontrado.")
    r = linha["detalhes"]
    calculado = codigo_integridade(r)
    r.setdefault("integridade", calculado)  # análises anteriores ao campo
    # Recalcula sobre o conteúdo gravado: se alguém alterar o laudo no banco, a verificação acusa
    r["integridade_conferida"] = calculado == r["integridade"]
    return r


@app.get("/api/analises/{protocolo}/laudo.pdf")
def laudo_pdf(protocolo: str, baixar: bool = False):
    """Laudo em PDF, sempre gerado a partir da análise gravada (nunca de dados enviados pelo navegador)."""
    r = _analise_gravada(protocolo)
    pdf = gerar_pdf(r, f"{URL_PUBLICA}/verificar/{r['protocolo']}", _imagens(r["protocolo"]))
    modo = "attachment" if baixar else "inline"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'{modo}; filename="laudo-{r["protocolo"]}.pdf"'})


def _imagens(protocolo: str) -> dict[str, bytes]:
    with pool.connection() as conn:
        linhas = conn.execute("SELECT tipo, png FROM geo.analises_imagens WHERE protocolo = %s",
                              (protocolo,)).fetchall()
    return {l["tipo"]: bytes(l["png"]) for l in linhas}


@app.get("/api/analises/{protocolo}/imagens/{tipo}.png")
def imagem_satelite(protocolo: str, tipo: str):
    """Recorte Sentinel-2 de antes ou depois gravado com a análise."""
    if tipo not in ("antes", "depois"):
        raise HTTPException(404, "Imagem inexistente.")
    png = _imagens(protocolo.strip().upper()).get(tipo)
    if not png:
        raise HTTPException(404, "Imagem não disponível para este protocolo.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.get("/api/analises/{protocolo}/eudr.geojson")
def exportar_eudr(protocolo: str, u: dict = Depends(usuario)):
    """Geolocalização da área no formato GeoJSON da declaração de diligência do EUDR (sistema TRACES da UE)."""
    r = _analise_gravada(protocolo)
    return Response(json.dumps(geojson_eudr(r), ensure_ascii=False, indent=1), media_type="application/geo+json",
                    headers={"Content-Disposition": f'attachment; filename="eudr-{r["protocolo"]}.geojson"'})


@app.get("/verificar/{protocolo}", include_in_schema=False)
def pagina_verificacao(protocolo: str):
    """Destino do QR code: página pública que confere o laudo pelo protocolo."""
    return FileResponse(os.path.join(WEB_DIR, "verificar.html"), headers={"Cache-Control": "no-cache"})


# ---------- Carteira ----------
@app.get("/api/carteira")
def carteira(u: dict = Depends(usuario)):
    with pool.connection() as conn:
        return {"resumo": cart.resumo_carteira(conn, u["empresa_id"]), "areas": cart.listar_areas(conn, u["empresa_id"])}


@app.get("/api/carteira/painel")
def painel_carteira(u: dict = Depends(usuario)):
    """Dashboard: evolução semanal, motivos, municípios, fornecedores e validade dos laudos."""
    with pool.connection() as conn:
        return {**painel.painel(conn, u["empresa_id"]), "resumo": cart.resumo_carteira(conn, u["empresa_id"]),
                "uso": licencas.uso(conn, u["empresa_id"]), "risco": mercado.risco_carteira(conn, u["empresa_id"])}


@app.post("/api/carteira/areas/{area_id}/convite")
def convidar_produtor(area_id: int, request: Request, u: dict = Depends(usuario)):
    """Link para o fornecedor desta área ver a situação dela e aceitar o laudo e o selo."""
    with pool.connection() as conn:
        c = convites.gerar(conn, u["empresa_id"], area_id, u["id"])
        if not c:
            raise HTTPException(404, "Área não encontrada.")
        contas.auditar(conn, "convite_produtor", u, alvo=str(area_id), ip=_ip(request))
    return c


@app.get("/api/carteira/convites.csv")
def convites_em_lote(request: Request, u: dict = Depends(usuario)):
    """Um convite por área ativa (reaproveita os válidos), em planilha para mala direta ou WhatsApp."""
    with pool.connection() as conn:
        areas = conn.execute("""SELECT id, nome, fornecedor, codigo_externo FROM app.areas
                                WHERE empresa_id = %s AND ativa ORDER BY nome""", (u["empresa_id"],)).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Área", "Fornecedor", "Código no ERP", "Link do convite", "Mensagem sugerida", "Já aceito"])
        for a in areas:
            c = convites.gerar(conn, u["empresa_id"], a["id"], u["id"])
            w.writerow([a["nome"], a["fornecedor"] or "", a["codigo_externo"] or "", c["url"], c["mensagem"],
                        "sim" if c["aceito"] else "não"])
        contas.auditar(conn, "convite_produtor", u, alvo="lote", detalhes={"areas": len(areas)}, ip=_ip(request))
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="convites-produtores.csv"'})


@app.get("/api/carteira/mapa.geojson")
def mapa_carteira(u: dict = Depends(usuario)):
    with pool.connection() as conn:
        return painel.mapa(conn, u["empresa_id"])


class PedidoArea(BaseModel):
    nome: str | None = Field(None, max_length=200)
    geometria: dict[str, Any] | None = None
    imovel_id: int | None = None
    fornecedor: str | None = Field(None, max_length=200)
    codigo_externo: str | None = Field(None, max_length=100)
    reserva_legal_ha: float | None = Field(None, ge=0)


@app.post("/api/carteira/areas")
def adicionar_area(p: PedidoArea, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        try:
            area_id = cart.adicionar_area(conn, u["empresa_id"], u["id"], nome=p.nome, geometria=p.geometria,
                                          imovel_id=p.imovel_id, fornecedor=p.fornecedor,
                                          codigo_externo=p.codigo_externo, reserva_legal_ha=p.reserva_legal_ha,
                                          origem="cadastro manual")
        except cart.ErroCarteira as e:
            raise HTTPException(422, str(e)) from e
        contas.auditar(conn, "carteira_area", u, alvo=p.nome, ip=_ip(request))
    return {"id": area_id}


@app.post("/api/carteira/importar")
async def importar(request: Request, arquivo: UploadFile = File(...), u: dict = Depends(usuario)):
    """Lote: planilha (CSV/XLSX com códigos SIGEF/CAR) ou arquivo geográfico (cada polígono vira uma área)."""
    conteudo = await arquivo.read(TAMANHO_MAX_ARQUIVO + 1)
    if len(conteudo) > TAMANHO_MAX_ARQUIVO:
        raise HTTPException(413, "Arquivo maior que 20 MB.")
    nome = arquivo.filename or "arquivo"
    planilha = nome.lower().endswith((".csv", ".xlsx", ".txt"))
    try:
        itens = ler_planilha(nome, conteudo) if planilha else ler_feicoes(nome, conteudo)
    except (ErroPlanilha, ErroArquivo) as e:
        raise HTTPException(422, str(e)) from e

    def gravar():
        with pool.connection() as conn:
            fn = cart.importar_planilha if planilha else cart.importar_feicoes
            r = fn(conn, u["empresa_id"], u["id"], itens, nome)
            contas.auditar(conn, "carteira_importada", u, alvo=nome,
                           detalhes={"importadas": r["importadas"], "ignoradas": len(r["ignoradas"])}, ip=_ip(request))
            return r

    return await run_in_threadpool(gravar)


class PedidoIds(BaseModel):
    ids: list[int] | None = None


@app.post("/api/carteira/reanalisar")
def reanalisar(p: PedidoIds, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        ids = p.ids or [r["id"] for r in conn.execute(
            "SELECT id FROM app.areas WHERE empresa_id = %s AND ativa", (u["empresa_id"],)).fetchall()]
        n = cart.enfileirar(conn, u["empresa_id"], ids, u["id"], "manual")
        contas.auditar(conn, "carteira_reanalise", u, detalhes={"areas": n}, ip=_ip(request))
    return {"na_fila": n}


@app.delete("/api/carteira/areas/{area_id}")
def remover_area(area_id: int, request: Request, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        a = conn.execute("UPDATE app.areas SET ativa = false WHERE id = %s AND empresa_id = %s AND ativa RETURNING nome",
                         (area_id, u["empresa_id"])).fetchone()
        if not a:
            raise HTTPException(404, "Área não encontrada.")
        conn.execute("DELETE FROM app.tarefas WHERE area_id = %s AND estado = 'pendente'", (area_id,))
        contas.auditar(conn, "carteira_removida", u, alvo=a["nome"], ip=_ip(request))
    return {"ok": True}


@app.get("/api/carteira/areas/{area_id}/geometria")
def geometria_area(area_id: int, u: dict = Depends(usuario)):
    with pool.connection() as conn:
        a = conn.execute("SELECT nome, ST_AsGeoJSON(geom, 6) AS g FROM app.areas WHERE id = %s AND empresa_id = %s",
                         (area_id, u["empresa_id"])).fetchone()
    if not a:
        raise HTTPException(404, "Área não encontrada.")
    return {"type": "Feature", "properties": {"nome": a["nome"]}, "geometry": json.loads(a["g"])}


@app.get("/api/carteira/alertas")
def alertas(u: dict = Depends(usuario)):
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT al.id, al.criado_em, al.de, al.para, al.protocolo, al.resumo, al.lido_em, a.nome, a.fornecedor
               FROM app.alertas al JOIN app.areas a ON a.id = al.area_id
               WHERE al.empresa_id = %s ORDER BY al.criado_em DESC LIMIT 200""", (u["empresa_id"],)).fetchall()
    return [{**l, "criado_em": l["criado_em"].isoformat(), "lido_em": l["lido_em"].isoformat() if l["lido_em"] else None}
            for l in linhas]


@app.post("/api/carteira/alertas/lidos")
def marcar_lidos(u: dict = Depends(usuario)):
    with pool.connection() as conn:
        conn.execute("UPDATE app.alertas SET lido_em = now() WHERE empresa_id = %s AND lido_em IS NULL",
                     (u["empresa_id"],))
    return {"ok": True}


@app.get("/api/carteira/exportar.csv")
def exportar_carteira(u: dict = Depends(usuario)):
    """Planilha da carteira (abre direto no Excel: separador ';' e BOM UTF-8)."""
    from .regras import ROTULOS
    with pool.connection() as conn:
        areas = cart.listar_areas(conn, u["empresa_id"])
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Nome", "Fornecedor", "Código no ERP", "Município", "Área (ha)", "Situação", "Situação anterior",
                "Analisado em", "Protocolo", "Laudo"])
    for a in areas:
        w.writerow([a["nome"], a["fornecedor"] or "", a["codigo_externo"] or "", a["municipio"] or "",
                    f"{a['area_ha']:.2f}".replace(".", ",") if a["area_ha"] is not None else "",
                    ROTULOS.get(a["ultimo_status"], "Aguardando análise"), ROTULOS.get(a["status_anterior"], ""),
                    a["analisado_em"][:10] if a["analisado_em"] else "", a["ultimo_protocolo"] or "",
                    f"{URL_PUBLICA}/api/analises/{a['ultimo_protocolo']}/laudo.pdf" if a["ultimo_protocolo"] else ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="carteira-geolem.csv"'})


@app.get("/api/carteira/eudr.geojson")
def exportar_carteira_eudr(u: dict = Depends(usuario)):
    """Geolocalização de todas as áreas Conformes da carteira, num único arquivo para a declaração do EUDR."""
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT an.detalhes FROM app.areas a JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo
               WHERE a.empresa_id = %s AND a.ativa AND a.ultimo_status = 'ok' ORDER BY a.nome""",
            (u["empresa_id"],)).fetchall()
    fc = {"type": "FeatureCollection", "features": [f for l in linhas for f in geojson_eudr(l["detalhes"])["features"]]}
    return Response(json.dumps(fc, ensure_ascii=False), media_type="application/geo+json",
                    headers={"Content-Disposition": 'attachment; filename="eudr-carteira-conformes.geojson"'})


# ---------- Arquivos ----------
@app.post("/api/arquivos/geometria")
async def arquivo_para_geometria(arquivo: UploadFile = File(...)):
    conteudo = await arquivo.read(TAMANHO_MAX_ARQUIVO + 1)
    if len(conteudo) > TAMANHO_MAX_ARQUIVO:
        raise HTTPException(413, "Arquivo maior que 20 MB.")
    try:
        return ler_geometria(arquivo.filename or "arquivo", conteudo)
    except ErroArquivo as e:
        raise HTTPException(422, str(e)) from e


# ---------- Vector tiles ----------
@app.get("/api/tiles/{camada}/{z}/{x}/{y}.pbf")
def tile(camada: str, z: int, x: int, y: int):
    if camada not in tiles.CAMADAS:
        raise HTTPException(404, "Camada inexistente.")
    if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(400, "Tile fora dos limites.")
    with pool.connection() as conn:
        dados = tiles.gerar_tile(conn, camada, z, x, y)
    return Response(dados, media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "private, max-age=3600"})


# ---------- Frontend ----------
class FrontendSemCache(StaticFiles):
    """Obriga o navegador a revalidar (ETag) para que o cliente sempre veja a versão nova após atualizações."""

    async def get_response(self, path, scope):
        resposta = await super().get_response(path, scope)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


if os.path.isdir(WEB_DIR):
    app.mount("/", FrontendSemCache(directory=WEB_DIR, html=True), name="web")
