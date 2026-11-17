import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import tiles
from .analise import ErroAnalise, analisar
from .arquivos import ErroArquivo, ler_geometria
from .config import REGIAO_BBOX, TAMANHO_MAX_ARQUIVO, URL_PUBLICA, WEB_DIR
from .laudo_pdf import codigo_integridade, gerar_pdf
from .db import pool


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(title="GeoLEM API", version="0.2.0", lifespan=ciclo_de_vida,
              description="Compliance socioambiental de áreas rurais no oeste da Bahia.")

COLUNAS_IMOVEL = "id, tipo, fonte, codigo, nome, municipio, uf, situacao, area_ha, reserva_legal_ha"


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


def _carregar_imovel(conn, imovel_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {COLUNAS_IMOVEL}, ST_AsGeoJSON(geom, 6) AS geom_json FROM geo.imoveis WHERE id = %s", (imovel_id,)
    ).fetchone()


@app.get("/api/imoveis/{imovel_id}")
def obter_imovel(imovel_id: int):
    with pool.connection() as conn:
        im = _carregar_imovel(conn, imovel_id)
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
def criar_analise(p: PedidoAnalise):
    with pool.connection() as conn:
        imovel = None
        geometria = p.geometria
        if p.imovel_id is not None:
            im = _carregar_imovel(conn, p.imovel_id)
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
            return analisar(conn, geometria, nome=p.nome, imovel=imovel, reserva_legal_ha=p.reserva_legal_ha)
        except ErroAnalise as e:
            conn.rollback()
            raise HTTPException(422, str(e)) from e


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
    pdf = gerar_pdf(r, f"{URL_PUBLICA}/verificar/{r['protocolo']}")
    modo = "attachment" if baixar else "inline"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'{modo}; filename="laudo-{r["protocolo"]}.pdf"'})


@app.get("/verificar/{protocolo}", include_in_schema=False)
def pagina_verificacao(protocolo: str):
    """Destino do QR code: página pública que confere o laudo pelo protocolo."""
    return FileResponse(os.path.join(WEB_DIR, "verificar.html"), headers={"Cache-Control": "no-cache"})


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
                    headers={"Cache-Control": "public, max-age=3600"})


# ---------- Frontend ----------
class FrontendSemCache(StaticFiles):
    """Obriga o navegador a revalidar (ETag) para que o cliente sempre veja a versão nova após atualizações."""

    async def get_response(self, path, scope):
        resposta = await super().get_response(path, scope)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


if os.path.isdir(WEB_DIR):
    app.mount("/", FrontendSemCache(directory=WEB_DIR, html=True), name="web")
