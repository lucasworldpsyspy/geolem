"""Mercado de lotes de origem verificada.

Quem tem grão (cooperativa, armazém, grupo produtor) anuncia um lote ligado a uma área da própria carteira, já
analisada pelo GeoLEM. Compradores filtram por cultura, município e situação socioambiental e manifestam interesse.
O vendedor aceita ou recusa; só no aceite os dois lados veem nome e contato um do outro. O GeoLEM conecta e registra
o fechamento (base da futura taxa de intermediação), mas não opera pagamento, frete nem classificação.
"""
from datetime import date, datetime, timedelta, timezone

from . import mercado, regras

ANALISE_MAX_DIAS = 90         # lote só com análise recente
SITUACOES_LOTE = ("ativo", "negociando", "vendido", "cancelado")


class ErroMercado(ValueError):
    pass


def _verificacao(conn, area_id: int, empresa_id: int) -> dict:
    a = conn.execute(
        """SELECT a.id, a.nome, a.cultura, a.municipio, a.area_ha, a.ultimo_status, a.ultimo_protocolo, a.analisado_em,
                  an.detalhes
           FROM app.areas a LEFT JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo
           WHERE a.id = %s AND a.empresa_id = %s AND a.ativa""", (area_id, empresa_id)).fetchone()
    if not a:
        raise ErroMercado("Área não encontrada na sua carteira.")
    if not a["ultimo_status"] or not a["analisado_em"]:
        raise ErroMercado("A área ainda não foi analisada. Aguarde a análise para anunciar.")
    if a["ultimo_status"] == "bad":
        raise ErroMercado("Área com resultado Não conforme não pode ser anunciada no mercado de origem verificada.")
    if a["analisado_em"] < datetime.now(timezone.utc) - timedelta(days=ANALISE_MAX_DIAS):
        raise ErroMercado(f"A análise da área tem mais de {ANALISE_MAX_DIAS} dias. Peça a reanálise antes de anunciar.")
    return a


def anunciar(conn, empresa_id: int, usuario_id: int, *, area_id: int, safra: str, volume_t: float,
             preco_pedido: float | None, entrega: str | None, disponivel_ate: date | None, observacoes: str | None) -> int:
    a = _verificacao(conn, area_id, empresa_id)
    if volume_t <= 0:
        raise ErroMercado("Informe o volume em toneladas.")
    ref = mercado.referencias(conn).get(a["cultura"]) or {}
    if ref.get("produtividade_sc_ha"):
        teto = (a["area_ha"] or 0) * ref["produtividade_sc_ha"] * ref["kg_por_unidade"] / 1000 * 1.5
        if volume_t > max(teto, 1):
            raise ErroMercado(f"Volume acima do que a área produz numa safra (cerca de {teto / 1.5:,.0f} t).".replace(",", "."))
    return conn.execute(
        """INSERT INTO app.lotes (empresa_id, area_id, criado_por, cultura, safra, volume_t, preco_pedido, entrega,
                                  disponivel_ate, observacoes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (empresa_id, area_id, usuario_id, a["cultura"], safra.strip()[:20], volume_t, preco_pedido,
         (entrega or "").strip()[:120] or None, disponivel_ate, (observacoes or "").strip()[:1000] or None),
    ).fetchone()["id"]


def _selo(l: dict) -> dict:
    checks = (l.get("detalhes") or {}).get("checks", [])
    rb = next((c for c in checks if c["id"] == "renovabio"), None)
    return {"status": l["ultimo_status"], "rotulo": regras.ROTULOS.get(l["ultimo_status"], "-"),
            "ocorrencias": [c["titulo"] for c in checks if c["status"] in ("warn", "bad") and regras.entra_no_resultado(c)],
            "renovabio": rb.get("rotulo") if rb else None, "renovabio_elegivel": bool(rb and rb["status"] == "ok"),
            "analisado_em": l["analisado_em"].isoformat() if l["analisado_em"] else None,
            "protocolo": l["ultimo_protocolo"]}


SQL_LOTE = """SELECT l.*, a.municipio, a.area_ha, a.ultimo_status, a.ultimo_protocolo, a.analisado_em, an.detalhes,
                     e.nome AS empresa_nome
              FROM app.lotes l JOIN app.areas a ON a.id = l.area_id JOIN app.empresas e ON e.id = l.empresa_id
              LEFT JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo"""


def _lote_publico(l: dict, refs: dict, empresa_id: int) -> dict:
    ref = refs.get(l["cultura"]) or {}
    return {
        "id": l["id"], "cultura": l["cultura"], "cultura_rotulo": ref.get("rotulo", l["cultura"]),
        "unidade": ref.get("unidade"), "safra": l["safra"], "volume_t": float(l["volume_t"]),
        "preco_pedido": float(l["preco_pedido"]) if l["preco_pedido"] is not None else None,
        "preco_referencia": ref.get("preco_saca"), "fonte_referencia": ref.get("fonte_preco"),
        "entrega": l["entrega"], "disponivel_ate": l["disponivel_ate"].isoformat() if l["disponivel_ate"] else None,
        "observacoes": l["observacoes"], "situacao": l["situacao"], "municipio": l["municipio"],
        "area_ha": round(l["area_ha"] or 0), "verificacao": _selo(l),
        "meu": l["empresa_id"] == empresa_id, "criado_em": l["criado_em"].isoformat(),
    }


def listar(conn, empresa_id: int, cultura: str | None = None, municipio: str | None = None,
           so_conformes: bool = False, renovabio: bool = False) -> list[dict]:
    refs = mercado.referencias(conn)
    linhas = conn.execute(
        f"""{SQL_LOTE} WHERE l.situacao IN ('ativo', 'negociando') AND a.ativa
              AND (l.disponivel_ate IS NULL OR l.disponivel_ate >= current_date)
              AND a.ultimo_status <> 'bad'
              AND (%(c)s::text IS NULL OR l.cultura = %(c)s)
              AND (%(m)s::text IS NULL OR unaccent(a.municipio) ILIKE unaccent(%(mp)s))
              AND (NOT %(ok)s OR a.ultimo_status = 'ok')
            ORDER BY l.criado_em DESC LIMIT 300""",
        {"c": cultura, "m": municipio, "mp": f"%{municipio}%" if municipio else None, "ok": so_conformes}).fetchall()
    saida = [_lote_publico(l, refs, empresa_id) for l in linhas]
    if renovabio:
        saida = [s for s in saida if s["verificacao"]["renovabio_elegivel"]]
    meus_interesses = {r["lote_id"]: r["situacao"] for r in conn.execute(
        "SELECT lote_id, situacao FROM app.interesses WHERE empresa_id = %s", (empresa_id,)).fetchall()}
    for s in saida:
        s["meu_interesse"] = meus_interesses.get(s["id"])
    return saida


def manifestar(conn, empresa_id: int, usuario_id: int, lote_id: int, volume_t: float | None, mensagem: str | None) -> int:
    l = conn.execute("SELECT empresa_id, situacao FROM app.lotes WHERE id = %s", (lote_id,)).fetchone()
    if not l or l["situacao"] not in ("ativo", "negociando"):
        raise ErroMercado("Lote indisponível.")
    if l["empresa_id"] == empresa_id:
        raise ErroMercado("Este lote é da sua empresa.")
    r = conn.execute(
        """INSERT INTO app.interesses (lote_id, empresa_id, usuario_id, volume_t, mensagem)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (lote_id, empresa_id) DO UPDATE SET volume_t = EXCLUDED.volume_t, mensagem = EXCLUDED.mensagem,
             situacao = CASE WHEN app.interesses.situacao IN ('recusado', 'cancelado') THEN 'enviado'
                             ELSE app.interesses.situacao END
           RETURNING id""", (lote_id, empresa_id, usuario_id, volume_t, (mensagem or "").strip()[:1000] or None)).fetchone()
    return r["id"]


def _contato(conn, usuario_id: int | None, empresa_id: int) -> dict:
    u = conn.execute(
        """SELECT u.nome, u.email, e.nome AS empresa, e.telefone FROM app.empresas e
           LEFT JOIN app.usuarios u ON u.id = %s WHERE e.id = %s""", (usuario_id, empresa_id)).fetchone()
    return {"empresa": u["empresa"], "nome": u["nome"], "email": u["email"], "telefone": u["telefone"]}


def meus(conn, empresa_id: int) -> dict:
    refs = mercado.referencias(conn)
    lotes = [_lote_publico(l, refs, empresa_id) for l in conn.execute(
        f"{SQL_LOTE} WHERE l.empresa_id = %s ORDER BY l.criado_em DESC", (empresa_id,)).fetchall()]
    recebidos = conn.execute(
        """SELECT i.*, e.nome AS comprador FROM app.interesses i JOIN app.lotes l ON l.id = i.lote_id
           JOIN app.empresas e ON e.id = i.empresa_id WHERE l.empresa_id = %s ORDER BY i.criado_em DESC""",
        (empresa_id,)).fetchall()
    por_lote: dict[int, list] = {}
    for i in recebidos:
        item = {"id": i["id"], "volume_t": float(i["volume_t"]) if i["volume_t"] is not None else None,
                "mensagem": i["mensagem"], "situacao": i["situacao"], "criado_em": i["criado_em"].isoformat()}
        # Identidade e contato do comprador só depois do aceite
        if i["situacao"] in ("aceito", "fechado"):
            item["contato"] = _contato(conn, i["usuario_id"], i["empresa_id"])
        else:
            item["contato"] = None
        por_lote.setdefault(i["lote_id"], []).append(item)
    for l in lotes:
        l["interesses"] = por_lote.get(l["id"], [])
    enviados = []
    for i in conn.execute(
            f"""SELECT i.*, l.cultura, l.safra, l.volume_t AS volume_lote, l.empresa_id AS vendedor_id, l.criado_por,
                       a.municipio FROM app.interesses i JOIN app.lotes l ON l.id = i.lote_id
                JOIN app.areas a ON a.id = l.area_id WHERE i.empresa_id = %s ORDER BY i.criado_em DESC""",
            (empresa_id,)).fetchall():
        enviados.append({"id": i["id"], "lote_id": i["lote_id"], "cultura": refs.get(i["cultura"], {}).get("rotulo", i["cultura"]),
                         "safra": i["safra"], "municipio": i["municipio"], "volume_lote": float(i["volume_lote"]),
                         "volume_t": float(i["volume_t"]) if i["volume_t"] is not None else None,
                         "situacao": i["situacao"], "criado_em": i["criado_em"].isoformat(),
                         "contato": _contato(conn, i["criado_por"], i["vendedor_id"]) if i["situacao"] in ("aceito", "fechado") else None})
    return {"lotes": lotes, "interesses_enviados": enviados}


def responder(conn, empresa_id: int, interesse_id: int, acao: str, volume_t: float | None = None,
              preco: float | None = None) -> None:
    i = conn.execute(
        """SELECT i.id, i.situacao, i.lote_id FROM app.interesses i JOIN app.lotes l ON l.id = i.lote_id
           WHERE i.id = %s AND l.empresa_id = %s""", (interesse_id, empresa_id)).fetchone()
    if not i:
        raise ErroMercado("Interesse não encontrado.")
    if acao == "aceitar":
        conn.execute("UPDATE app.interesses SET situacao = 'aceito', respondido_em = now() WHERE id = %s", (i["id"],))
        conn.execute("UPDATE app.lotes SET situacao = 'negociando', atualizado_em = now() WHERE id = %s AND situacao = 'ativo'",
                     (i["lote_id"],))
    elif acao == "recusar":
        conn.execute("UPDATE app.interesses SET situacao = 'recusado', respondido_em = now() WHERE id = %s", (i["id"],))
    elif acao == "fechar":
        if i["situacao"] != "aceito":
            raise ErroMercado("Só um interesse aceito pode ser fechado.")
        conn.execute("""UPDATE app.interesses SET situacao = 'fechado', fechado_em = now(), volume_fechado_t = %s,
                        preco_fechado = %s WHERE id = %s""", (volume_t, preco, i["id"]))
        conn.execute("UPDATE app.lotes SET situacao = 'vendido', atualizado_em = now() WHERE id = %s", (i["lote_id"],))
    else:
        raise ErroMercado("Ação inválida.")


def alterar_lote(conn, empresa_id: int, lote_id: int, situacao: str) -> None:
    if situacao not in SITUACOES_LOTE:
        raise ErroMercado("Situação inválida.")
    if not conn.execute("UPDATE app.lotes SET situacao = %s, atualizado_em = now() WHERE id = %s AND empresa_id = %s",
                        (situacao, lote_id, empresa_id)).rowcount:
        raise ErroMercado("Lote não encontrado.")


def resumo_publico(conn) -> dict:
    """Vitrine do observatório: volume disponível por cultura, sem identificar ninguém."""
    refs = mercado.referencias(conn)
    linhas = conn.execute(
        """SELECT l.cultura, count(*) AS lotes, sum(l.volume_t) AS volume_t
           FROM app.lotes l JOIN app.areas a ON a.id = l.area_id
           WHERE l.situacao IN ('ativo', 'negociando') AND a.ativa AND a.ultimo_status <> 'bad'
             AND (l.disponivel_ate IS NULL OR l.disponivel_ate >= current_date)
           GROUP BY l.cultura ORDER BY sum(l.volume_t) DESC""").fetchall()
    return {"culturas": [{"cultura": refs.get(l["cultura"], {}).get("rotulo", l["cultura"]), "lotes": l["lotes"],
                          "volume_t": float(l["volume_t"])} for l in linhas],
            "lotes": sum(l["lotes"] for l in linhas), "volume_t": float(sum(l["volume_t"] for l in linhas))}
