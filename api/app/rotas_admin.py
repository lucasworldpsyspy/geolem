"""Administração da plataforma (só superadmin): visão do negócio, CRM, clientes e licenças, leads e uso."""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import contas, licencas, mercado, observatorio
from .db import pool
from .dependencias import ip as ip_de
from .dependencias import superadmin
from .planilha import ErroPlanilha, _chave, _linhas

rotas = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(superadmin)])

ETAPAS = ["novo", "contato", "reuniao", "piloto", "proposta", "cliente", "perdido"]
TIPOS = ["armazem", "revenda", "trading", "cooperativa", "banco", "consultoria", "agroindustria",
         "produtor", "comprador_graos", "investidor_anjo", "rede_anjos", "fundo_vc", "cvc", "aceleradora", "fomento",
         "marketplace", "associacao", "outro"]
CATEGORIAS = ["cliente", "produtor", "comprador", "investidor", "parceiro"]
# Categoria sugerida pelo tipo, quando a planilha ou o lead não informam
CATEGORIA_DO_TIPO = {"produtor": "produtor", "comprador_graos": "comprador", "investidor_anjo": "investidor",
                     "rede_anjos": "investidor", "fundo_vc": "investidor", "cvc": "investidor", "fomento": "investidor",
                     "aceleradora": "parceiro", "marketplace": "parceiro", "associacao": "parceiro",
                     "consultoria": "parceiro"}


def _iso(linhas):
    for l in linhas:
        for k, v in list(l.items()):
            if hasattr(v, "isoformat"):
                l[k] = v.isoformat()
            elif hasattr(v, "is_finite"):
                l[k] = float(v)
    return linhas


# ---------- visão geral ----------
@rotas.get("/resumo")
def resumo():
    with pool.connection() as conn:
        r = conn.execute(
            """SELECT
                 (SELECT coalesce(sum(valor_mensal), 0) FROM app.empresas WHERE situacao = 'ativa' AND NOT operadora) AS mrr,
                 (SELECT count(*) FROM app.empresas WHERE situacao = 'ativa' AND NOT operadora) AS clientes_ativos,
                 (SELECT count(*) FROM app.empresas WHERE situacao = 'teste' AND NOT operadora
                    AND (valido_ate IS NULL OR valido_ate >= current_date)) AS em_teste,
                 (SELECT count(*) FROM app.empresas WHERE NOT operadora AND situacao IN ('teste', 'ativa')
                    AND valido_ate BETWEEN current_date AND current_date + 7) AS vencendo_7d,
                 (SELECT count(*) FROM app.empresas WHERE NOT operadora AND situacao IN ('teste', 'ativa')
                    AND valido_ate < current_date) AS vencidas,
                 (SELECT count(*) FROM app.leads WHERE criado_em >= now() - interval '30 days') AS leads_30d,
                 (SELECT count(*) FROM app.leads WHERE prospect_id IS NULL) AS leads_sem_prospect,
                 (SELECT count(*) FROM geo.analises WHERE criado_em >= date_trunc('month', now())) AS laudos_mes,
                 (SELECT count(*) FROM app.areas WHERE ativa) AS areas_monitoradas,
                 (SELECT coalesce(sum(valor_estimado), 0) FROM app.prospects
                    WHERE etapa NOT IN ('cliente', 'perdido') AND categoria IN ('cliente', 'comprador')) AS pipeline_valor,
                 (SELECT count(*) FROM app.prospects WHERE proximo_em <= current_date
                    AND etapa NOT IN ('cliente', 'perdido')) AS tarefas_atrasadas""").fetchone()
        funil = conn.execute(
            """SELECT etapa, count(*) AS n, coalesce(sum(valor_estimado), 0) AS valor FROM app.prospects
               WHERE categoria IN ('cliente', 'comprador') GROUP BY etapa""").fetchall()
        categorias = conn.execute("SELECT categoria, count(*) AS n FROM app.prospects GROUP BY categoria").fetchall()
        laudos = conn.execute(
            """SELECT to_char(date_trunc('month', criado_em), 'YYYY-MM') AS mes, count(*) AS n FROM geo.analises
               WHERE criado_em >= date_trunc('month', now()) - interval '5 months' GROUP BY 1 ORDER BY 1""").fetchall()
        leads = conn.execute(
            """SELECT to_char(date_trunc('week', criado_em), 'YYYY-MM-DD') AS semana, interesse, count(*) AS n
               FROM app.leads WHERE criado_em >= now() - interval '12 weeks' GROUP BY 1, 2 ORDER BY 1""").fetchall()
    por_etapa = {f["etapa"]: f for f in funil}
    return {**_iso([dict(r)])[0],
            "funil": [{"etapa": e, "n": por_etapa.get(e, {}).get("n", 0),
                       "valor": float(por_etapa.get(e, {}).get("valor", 0))} for e in ETAPAS],
            "laudos_por_mes": laudos, "leads_por_semana": leads,
            "por_categoria": {c["categoria"]: c["n"] for c in categorias}}


# ---------- empresas clientes e licenças ----------
@rotas.get("/empresas")
def empresas():
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT e.id, e.nome, e.cnpj, e.plano, e.situacao, e.valido_ate, e.limite_areas, e.limite_usuarios,
                      e.limite_laudos_mes, e.valor_mensal, e.responsavel, e.email_contato, e.telefone, e.municipio,
                      e.observacoes, e.operadora, e.criado_em,
                      (SELECT count(*) FROM app.usuarios u WHERE u.empresa_id = e.id AND u.ativo) AS usuarios,
                      (SELECT count(*) FROM app.areas a WHERE a.empresa_id = e.id AND a.ativa) AS areas,
                      (SELECT count(*) FROM geo.analises an WHERE an.empresa_id = e.id
                         AND an.criado_em >= date_trunc('month', now())) AS laudos_mes,
                      (SELECT count(*) FROM geo.analises an WHERE an.empresa_id = e.id AND an.area_id IS NULL
                         AND an.criado_em >= date_trunc('month', now())) AS laudos_avulsos_mes,
                      (SELECT max(u.ultimo_acesso) FROM app.usuarios u WHERE u.empresa_id = e.id) AS ultimo_acesso
               FROM app.empresas e ORDER BY e.operadora DESC, e.nome""").fetchall()
    return {"empresas": _iso(linhas), "planos": licencas.PLANOS, "situacoes": licencas.SITUACOES}


class NovaEmpresa(BaseModel):
    nome: str = Field(..., max_length=160)
    cnpj: str | None = Field(None, max_length=20)
    plano: str = "piloto"
    admin_nome: str = Field(..., max_length=120)
    admin_email: str = Field(..., max_length=200)
    prospect_id: int | None = None


@rotas.post("/empresas")
def criar_empresa(p: NovaEmpresa, request: Request, u: dict = Depends(superadmin)):
    with pool.connection() as conn:
        try:
            campos = licencas.aplicar_plano(p.plano)
            eid = contas.criar_empresa(conn, p.nome, p.cnpj)
            situacao = "teste" if p.plano == "piloto" else "ativa"
            conn.execute(
                f"""UPDATE app.empresas SET plano = %s, situacao = %s, email_contato = %s, responsavel = %s,
                       {", ".join(f"{k} = %s" for k in campos)} WHERE id = %s""",
                (p.plano, situacao, p.admin_email, p.admin_nome, *campos.values(), eid))
            _, senha = contas.criar_usuario(conn, eid, p.admin_nome, p.admin_email, "admin")
        except (contas.ErroConta, licencas.ErroLicenca) as e:
            raise HTTPException(422, str(e)) from e
        if p.prospect_id:
            conn.execute("""UPDATE app.prospects SET empresa_id = %s, etapa = CASE WHEN %s = 'piloto' THEN 'piloto'
                                ELSE 'cliente' END, atualizado_em = now() WHERE id = %s""", (eid, p.plano, p.prospect_id))
            conn.execute("INSERT INTO app.interacoes (prospect_id, tipo, texto, usuario_id) VALUES (%s, 'etapa', %s, %s)",
                         (p.prospect_id, f"Conta criada no plano {licencas.PLANOS[p.plano]['nome']}", u["id"]))
        contas.auditar(conn, "empresa_criada", u, alvo=p.nome, detalhes={"plano": p.plano}, ip=ip_de(request))
    return {"id": eid, "senha_provisoria": senha}


class Licenca(BaseModel):
    nome: str | None = Field(None, max_length=160)
    cnpj: str | None = Field(None, max_length=20)
    plano: str | None = None
    aplicar_limites_do_plano: bool = False
    situacao: str | None = None
    valido_ate: date | None = None
    sem_validade: bool = False
    limite_areas: int | None = Field(None, ge=0)
    limite_usuarios: int | None = Field(None, ge=0)
    limite_laudos_mes: int | None = Field(None, ge=0)
    sem_limites: bool = False
    valor_mensal: float | None = Field(None, ge=0)
    responsavel: str | None = Field(None, max_length=120)
    email_contato: str | None = Field(None, max_length=200)
    telefone: str | None = Field(None, max_length=40)
    municipio: str | None = Field(None, max_length=100)
    observacoes: str | None = Field(None, max_length=2000)


@rotas.patch("/empresas/{empresa_id}")
def alterar_licenca(empresa_id: int, p: Licenca, request: Request, u: dict = Depends(superadmin)):
    dados = p.model_dump(exclude_unset=True)
    for k in ("aplicar_limites_do_plano", "sem_validade", "sem_limites"):
        dados.pop(k, None)
    if p.situacao and p.situacao not in licencas.SITUACOES:
        raise HTTPException(422, "Situação inválida.")
    if p.plano:
        try:
            padrao = licencas.aplicar_plano(p.plano)
        except licencas.ErroLicenca as e:
            raise HTTPException(422, str(e)) from e
        if p.aplicar_limites_do_plano:
            dados.update({k: v for k, v in padrao.items() if k not in p.model_fields_set})
    if p.sem_validade:
        dados["valido_ate"] = None
    if p.sem_limites:
        dados.update(limite_areas=None, limite_usuarios=None, limite_laudos_mes=None)
    if not dados:
        return {"ok": True}
    with pool.connection() as conn:
        n = conn.execute(f"UPDATE app.empresas SET {', '.join(f'{k} = %s' for k in dados)} WHERE id = %s",
                         (*dados.values(), empresa_id)).rowcount
        if not n:
            raise HTTPException(404, "Empresa não encontrada.")
        if dados.get("situacao") in ("suspensa", "cancelada"):
            conn.execute("""DELETE FROM app.sessoes WHERE usuario_id IN
                            (SELECT id FROM app.usuarios WHERE empresa_id = %s)""", (empresa_id,))
        contas.auditar(conn, "licenca_alterada", u, alvo=str(empresa_id),
                       detalhes={k: str(v) for k, v in dados.items()}, ip=ip_de(request))
    return {"ok": True}


@rotas.get("/empresas/{empresa_id}/usuarios")
def usuarios_da_empresa(empresa_id: int):
    with pool.connection() as conn:
        return _iso(conn.execute(
            """SELECT id, nome, email, papel, ativo, trocar_senha, ultimo_acesso FROM app.usuarios
               WHERE empresa_id = %s ORDER BY nome""", (empresa_id,)).fetchall())


@rotas.post("/usuarios/{usuario_id}/redefinir-senha")
def redefinir_senha(usuario_id: int, request: Request, u: dict = Depends(superadmin)):
    with pool.connection() as conn:
        alvo = conn.execute("SELECT email FROM app.usuarios WHERE id = %s", (usuario_id,)).fetchone()
        if not alvo:
            raise HTTPException(404, "Usuário não encontrado.")
        senha = contas.redefinir_senha(conn, usuario_id)
        contas.auditar(conn, "senha_redefinida", u, alvo=alvo["email"], ip=ip_de(request))
    return {"senha_provisoria": senha}


# ---------- CRM ----------
class Prospect(BaseModel):
    nome: str | None = Field(None, max_length=160)
    categoria: str | None = None
    pais: str | None = Field(None, max_length=60)
    site: str | None = Field(None, max_length=200)
    ticket: str | None = Field(None, max_length=120)
    cnpj: str | None = Field(None, max_length=20)
    tipo: str | None = None
    municipio: str | None = Field(None, max_length=100)
    uf: str | None = Field(None, max_length=2)
    contato_nome: str | None = Field(None, max_length=120)
    contato_cargo: str | None = Field(None, max_length=120)
    email: str | None = Field(None, max_length=200)
    telefone: str | None = Field(None, max_length=40)
    origem: str | None = Field(None, max_length=30)
    etapa: str | None = None
    valor_estimado: float | None = Field(None, ge=0)
    proximo_passo: str | None = Field(None, max_length=300)
    proximo_em: date | None = None
    motivo_perda: str | None = Field(None, max_length=300)
    notas: str | None = Field(None, max_length=4000)


def _validar_prospect(p: Prospect):
    if p.etapa and p.etapa not in ETAPAS:
        raise HTTPException(422, "Etapa inválida.")
    if p.tipo and p.tipo not in TIPOS:
        raise HTTPException(422, "Tipo inválido.")
    if p.categoria and p.categoria not in CATEGORIAS:
        raise HTTPException(422, "Categoria inválida.")


@rotas.get("/prospects")
def prospects():
    with pool.connection() as conn:
        linhas = conn.execute(
            """SELECT p.*, (SELECT count(*) FROM app.leads l WHERE l.prospect_id = p.id) AS leads,
                      (SELECT max(em) FROM app.interacoes i WHERE i.prospect_id = p.id) AS ultima_interacao,
                      e.situacao AS empresa_situacao
               FROM app.prospects p LEFT JOIN app.empresas e ON e.id = p.empresa_id
               ORDER BY p.atualizado_em DESC""").fetchall()
    return {"prospects": _iso(linhas), "etapas": ETAPAS, "tipos": TIPOS, "categorias": CATEGORIAS}


@rotas.post("/prospects")
def criar_prospect(p: Prospect, u: dict = Depends(superadmin)):
    _validar_prospect(p)
    if not (p.nome or "").strip():
        raise HTTPException(422, "Informe o nome da empresa.")
    dados = {k: v for k, v in p.model_dump().items() if v is not None}
    dados.setdefault("origem", "manual")
    dados.setdefault("categoria", CATEGORIA_DO_TIPO.get(p.tipo or "", "cliente"))
    with pool.connection() as conn:
        pid = conn.execute(f"""INSERT INTO app.prospects ({', '.join(dados)}) VALUES ({', '.join(['%s'] * len(dados))})
                               RETURNING id""", tuple(dados.values())).fetchone()["id"]
    return {"id": pid}


@rotas.patch("/prospects/{prospect_id}")
def alterar_prospect(prospect_id: int, p: Prospect, u: dict = Depends(superadmin)):
    _validar_prospect(p)
    dados = p.model_dump(exclude_unset=True)
    if not dados:
        return {"ok": True}
    with pool.connection() as conn:
        antes = conn.execute("SELECT etapa FROM app.prospects WHERE id = %s", (prospect_id,)).fetchone()
        if not antes:
            raise HTTPException(404, "Prospect não encontrado.")
        conn.execute(f"UPDATE app.prospects SET {', '.join(f'{k} = %s' for k in dados)}, atualizado_em = now() "
                     "WHERE id = %s", (*dados.values(), prospect_id))
        if p.etapa and p.etapa != antes["etapa"]:
            conn.execute("INSERT INTO app.interacoes (prospect_id, tipo, texto, usuario_id) VALUES (%s, 'etapa', %s, %s)",
                         (prospect_id, f"Etapa: {antes['etapa']} → {p.etapa}", u["id"]))
    return {"ok": True}


@rotas.delete("/prospects/{prospect_id}")
def remover_prospect(prospect_id: int):
    with pool.connection() as conn:
        conn.execute("UPDATE app.leads SET prospect_id = NULL WHERE prospect_id = %s", (prospect_id,))
        if not conn.execute("DELETE FROM app.prospects WHERE id = %s", (prospect_id,)).rowcount:
            raise HTTPException(404, "Prospect não encontrado.")
    return {"ok": True}


@rotas.get("/prospects/{prospect_id}/interacoes")
def interacoes(prospect_id: int):
    with pool.connection() as conn:
        return _iso(conn.execute(
            """SELECT i.id, i.em, i.tipo, i.texto, u.nome AS usuario FROM app.interacoes i
               LEFT JOIN app.usuarios u ON u.id = i.usuario_id WHERE i.prospect_id = %s ORDER BY i.em DESC""",
            (prospect_id,)).fetchall())


class Interacao(BaseModel):
    tipo: str = "nota"
    texto: str = Field(..., min_length=1, max_length=4000)


@rotas.post("/prospects/{prospect_id}/interacoes")
def nova_interacao(prospect_id: int, p: Interacao, u: dict = Depends(superadmin)):
    if p.tipo not in ("nota", "ligacao", "email", "whatsapp", "reuniao"):
        raise HTTPException(422, "Tipo inválido.")
    with pool.connection() as conn:
        conn.execute("INSERT INTO app.interacoes (prospect_id, tipo, texto, usuario_id) VALUES (%s, %s, %s, %s)",
                     (prospect_id, p.tipo, p.texto.strip(), u["id"]))
        conn.execute("UPDATE app.prospects SET atualizado_em = now() WHERE id = %s", (prospect_id,))
    return {"ok": True}


COLUNAS_PROSPECT = {
    "nome": ("nome", "razao_social", "empresa", "nome_fantasia"), "cnpj": ("cnpj",),
    "tipo": ("tipo", "segmento"), "municipio": ("municipio", "cidade"), "uf": ("uf", "estado"),
    "contato_nome": ("contato", "contato_nome", "responsavel"), "contato_cargo": ("cargo", "contato_cargo"),
    "email": ("email", "e_mail"), "telefone": ("telefone", "celular", "whatsapp"),
    "valor_estimado": ("valor_estimado", "valor"), "notas": ("notas", "observacoes", "obs"),
    "categoria": ("categoria",), "pais": ("pais", "country"), "site": ("site", "website", "url"),
    "ticket": ("ticket", "cheque", "faixa_de_investimento"),
}


@rotas.post("/prospects/importar")
async def importar_prospects(arquivo: UploadFile = File(...)):
    """Planilha (CSV/XLSX) de possíveis clientes; ignora quem já existe com o mesmo CNPJ ou nome."""
    conteudo = await arquivo.read(5 * 1024 * 1024 + 1)
    try:
        linhas = [l for l in _linhas(arquivo.filename or "x.csv", conteudo) if any(c not in (None, "") for c in l)]
    except ErroPlanilha as e:
        raise HTTPException(422, str(e)) from e
    if len(linhas) < 2:
        raise HTTPException(422, "Planilha vazia.")
    titulos = [_chave(t or "") for t in linhas[0]]
    idx = {campo: next((i for i, t in enumerate(titulos) if t in nomes), None) for campo, nomes in COLUNAS_PROSPECT.items()}
    if idx["nome"] is None:
        raise HTTPException(422, "A planilha precisa de uma coluna 'nome' (ou 'razao_social').")
    novos, repetidos = 0, 0
    with pool.connection() as conn:
        for l in linhas[1:]:
            d = {c: (str(l[i]).strip() if i is not None and i < len(l) and l[i] not in (None, "") else None)
                 for c, i in idx.items()}
            if not d["nome"]:
                continue
            tipo = _chave(d["tipo"] or "")
            d["tipo"] = (tipo if tipo in TIPOS else
                         next((t for t in TIPOS if tipo.startswith(t[:5])), "outro" if d["tipo"] else None))
            cat = _chave(d["categoria"] or "")
            d["categoria"] = cat if cat in CATEGORIAS else CATEGORIA_DO_TIPO.get(d["tipo"] or "", "cliente")
            d["uf"] = (d["uf"] or "")[:2].upper() or None
            try:
                d["valor_estimado"] = float(str(d["valor_estimado"]).replace(".", "").replace(",", ".")) if d["valor_estimado"] else None
            except ValueError:
                d["valor_estimado"] = None
            existe = conn.execute(
                """SELECT 1 FROM app.prospects WHERE (%s::text IS NOT NULL AND regexp_replace(cnpj, '\\D', '', 'g') =
                       regexp_replace(%s, '\\D', '', 'g')) OR lower(nome) = lower(%s)""",
                (d["cnpj"], d["cnpj"] or "", d["nome"])).fetchone()
            if existe:
                repetidos += 1
                continue
            dados = {k: v for k, v in d.items() if v is not None}
            dados["origem"] = "planilha"
            conn.execute(f"INSERT INTO app.prospects ({', '.join(dados)}) VALUES ({', '.join(['%s'] * len(dados))})",
                         tuple(dados.values()))
            novos += 1
    return {"importados": novos, "repetidos": repetidos}


@rotas.get("/prospects/exportar.csv")
def exportar_prospects():
    with pool.connection() as conn:
        linhas = conn.execute("SELECT * FROM app.prospects ORDER BY nome").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    cab = ["categoria", "nome", "cnpj", "tipo", "municipio", "uf", "pais", "site", "ticket", "contato_nome",
           "contato_cargo", "email", "telefone", "origem", "etapa", "valor_estimado", "proximo_passo", "proximo_em",
           "notas"]
    w.writerow(cab)
    for l in linhas:
        w.writerow(["" if l[c] is None else l[c] for c in cab])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="prospects-geolem.csv"'})


# ---------- preços de referência ----------
@rotas.get("/referencias")
def referencias():
    with pool.connection() as conn:
        return mercado.referencias(conn)


class Referencia(BaseModel):
    praca: str = Field(..., max_length=100)
    preco_saca: float | None = Field(None, ge=0, le=10000)
    data_preco: date | None = None
    fonte_preco: str | None = Field(None, max_length=200)
    produtividade_sc_ha: float | None = Field(None, ge=0, le=1000)
    fonte_produtividade: str | None = Field(None, max_length=200)


@rotas.put("/referencias/{produto}")
def salvar_referencia(produto: str, p: Referencia, u: dict = Depends(superadmin)):
    if produto not in mercado.PRODUTOS:
        raise HTTPException(404, "Produto inválido.")
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO app.referencias_mercado (produto, praca, preco_saca, data_preco, fonte_preco,
                                                    produtividade_sc_ha, fonte_produtividade, atualizado_por)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (produto) DO UPDATE SET praca = EXCLUDED.praca, preco_saca = EXCLUDED.preco_saca,
                 data_preco = EXCLUDED.data_preco, fonte_preco = EXCLUDED.fonte_preco,
                 produtividade_sc_ha = EXCLUDED.produtividade_sc_ha, fonte_produtividade = EXCLUDED.fonte_produtividade,
                 atualizado_em = now(), atualizado_por = EXCLUDED.atualizado_por""",
            (produto, p.praca, p.preco_saca, p.data_preco, p.fonte_preco, p.produtividade_sc_ha,
             p.fonte_produtividade, u["id"]))
    return {"ok": True}


# ---------- planejamento de produtores por município ----------
@rotas.get("/produtores/municipios")
def produtores_por_municipio():
    """Números agregados por município (nenhum produtor identificado) cruzados com o que já está no CRM."""
    with pool.connection() as conn:
        base = observatorio.dados(conn)["municipios"]
        crm = conn.execute(
            """SELECT municipio, count(*) FILTER (WHERE categoria = 'produtor') AS produtores,
                      count(*) FILTER (WHERE categoria IN ('cliente', 'comprador', 'parceiro')) AS outros
               FROM app.prospects WHERE municipio IS NOT NULL GROUP BY municipio""").fetchall()
        leads = conn.execute(
            """SELECT municipio, count(*) AS n FROM app.leads WHERE municipio IS NOT NULL GROUP BY municipio""").fetchall()
        aceitos = conn.execute(
            """SELECT a.municipio, count(*) FILTER (WHERE c.aceito_em IS NOT NULL) AS aceitos, count(*) AS enviados
               FROM app.convites c JOIN app.areas a ON a.id = c.area_id GROUP BY a.municipio""").fetchall()
    def por_chave(linhas, campo):
        d = {}
        for l in linhas:
            k = observatorio.chave_municipio((l["municipio"] or "").split("/")[0])
            d[k] = d.get(k, 0) + (l[campo] or 0)
        return d
    prod, outros = por_chave(crm, "produtores"), por_chave(crm, "outros")
    nleads, acc, env = por_chave(leads, "n"), por_chave(aceitos, "aceitos"), por_chave(aceitos, "enviados")
    saida = []
    for m in base:
        k = observatorio.chave_municipio(m["municipio"])
        alcancados = prod.get(k, 0)
        saida.append({**m, "produtores_crm": alcancados, "outros_crm": outros.get(k, 0), "leads": nleads.get(k, 0),
                      "convites_enviados": env.get(k, 0), "convites_aceitos": acc.get(k, 0),
                      "cobertura_pct": round(100 * alcancados / m["imoveis"], 1) if m["imoveis"] else 0})
    # Prioridade: muitos imóveis, muita pressão de compliance (desmatamento pós-2020) e pouca cobertura ainda
    for s in saida:
        s["prioridade"] = round(s["imoveis"] * (1 + s["pct_pos2020"] / 100) * (1 - s["cobertura_pct"] / 100))
    return sorted(saida, key=lambda s: -s["prioridade"])


# ---------- leads do site ----------
@rotas.get("/leads")
def leads():
    with pool.connection() as conn:
        return _iso(conn.execute(
            """SELECT l.id, l.criado_em, l.nome, l.email, l.telefone, l.empresa, l.perfil, l.municipio, l.interesse,
                      l.detalhe, l.boletim, l.prospect_id, p.etapa AS prospect_etapa
               FROM app.leads l LEFT JOIN app.prospects p ON p.id = l.prospect_id
               ORDER BY l.criado_em DESC LIMIT 500""").fetchall())


@rotas.post("/leads/{lead_id}/prospect")
def lead_para_prospect(lead_id: int, u: dict = Depends(superadmin)):
    with pool.connection() as conn:
        l = conn.execute("SELECT * FROM app.leads WHERE id = %s", (lead_id,)).fetchone()
        if not l:
            raise HTTPException(404, "Lead não encontrado.")
        if l["prospect_id"]:
            return {"id": l["prospect_id"]}
        tipo = l["perfil"] if l["perfil"] in TIPOS else ("outro" if l["perfil"] else None)
        if l["interesse"] == "selo":
            tipo = "produtor"
        categoria = CATEGORIA_DO_TIPO.get(tipo or "", "cliente")
        pid = conn.execute(
            """INSERT INTO app.prospects (nome, tipo, categoria, municipio, contato_nome, email, telefone, origem, notas)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'site', %s) RETURNING id""",
            (l["empresa"] or l["nome"], tipo, categoria, l["municipio"], l["nome"], l["email"], l["telefone"],
             f"Lead do site ({l['interesse']}): {l['detalhe']}")).fetchone()["id"]
        conn.execute("UPDATE app.leads SET prospect_id = %s WHERE lower(email) = lower(%s)", (pid, l["email"]))
        conn.execute("INSERT INTO app.interacoes (prospect_id, tipo, texto, usuario_id) VALUES (%s, 'nota', %s, %s)",
                     (pid, f"Criado a partir de lead do site ({l['interesse']})", u["id"]))
    return {"id": pid}


@rotas.delete("/leads/{lead_id}")
def excluir_lead(lead_id: int):
    """Exclusão a pedido do titular (LGPD)."""
    with pool.connection() as conn:
        email = conn.execute("SELECT email FROM app.leads WHERE id = %s", (lead_id,)).fetchone()
        if not email:
            raise HTTPException(404, "Lead não encontrado.")
        n = conn.execute("DELETE FROM app.leads WHERE lower(email) = lower(%s)", (email["email"],)).rowcount
    return {"excluidos": n}
