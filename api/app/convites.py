"""Convite do comprador ao produtor: o caminho legal e sistemático para trazer produtores ao GeoLEM.

O cliente gera um link para o fornecedor de uma área da carteira. O produtor abre, vê a situação da própria área e,
se quiser, aceita (com consentimento registrado). Só então ele entra no CRM do GeoLEM, como produtor, ligado ao
comprador que o convidou. Sem aceite, nenhum dado do fornecedor sai da carteira do cliente.
"""
import secrets
from datetime import datetime, timedelta, timezone

from . import regras
from .config import URL_PUBLICA

VALIDADE = timedelta(days=30)
CONSENTIMENTO = ("Autorizo a GeoLEM a guardar meus dados de contato e a situação socioambiental da minha área para "
                 "me enviar o laudo e o selo de origem verificada, e para falar comigo sobre o serviço. Posso pedir a "
                 "exclusão a qualquer momento. (Política de Privacidade v1.0)")


def link(token: str) -> str:
    return f"{URL_PUBLICA}/convite.html?t={token}"


def mensagem(empresa: str, area: str, token: str) -> str:
    return (f"Olá! A {empresa} usa o GeoLEM para verificar a origem socioambiental dos grãos que compra. "
            f"Veja a situação da sua área ({area}) e receba o laudo e o selo de origem verificada: {link(token)}")


def gerar(conn, empresa_id: int, area_id: int, usuario_id: int | None) -> dict | None:
    """Reaproveita um convite ainda válido e não aceito da mesma área; senão cria outro."""
    area = conn.execute("""SELECT a.id, a.nome, e.nome AS empresa FROM app.areas a JOIN app.empresas e ON e.id = a.empresa_id
                           WHERE a.id = %s AND a.empresa_id = %s AND a.ativa""", (area_id, empresa_id)).fetchone()
    if not area:
        return None
    atual = conn.execute(
        """SELECT token, aceito_em FROM app.convites WHERE area_id = %s AND (aceito_em IS NOT NULL OR expira_em > now())
           ORDER BY id DESC LIMIT 1""", (area_id,)).fetchone()
    if atual:
        token = atual["token"]
    else:
        token = secrets.token_urlsafe(18)
        conn.execute("""INSERT INTO app.convites (token, empresa_id, area_id, criado_por, expira_em)
                        VALUES (%s, %s, %s, %s, %s)""",
                     (token, empresa_id, area_id, usuario_id, datetime.now(timezone.utc) + VALIDADE))
    return {"token": token, "url": link(token), "mensagem": mensagem(area["empresa"], area["nome"], token),
            "aceito": bool(atual and atual["aceito_em"])}


def ver(conn, token: str) -> dict | None:
    c = conn.execute(
        """SELECT c.id, c.expira_em, c.aceito_em, e.nome AS empresa, a.nome AS area, a.municipio, a.area_ha,
                  a.ultimo_status, a.ultimo_protocolo, an.detalhes
           FROM app.convites c JOIN app.areas a ON a.id = c.area_id JOIN app.empresas e ON e.id = c.empresa_id
           LEFT JOIN geo.analises an ON an.protocolo = a.ultimo_protocolo
           WHERE c.token = %s""", (token,)).fetchone()
    if not c:
        return None
    conn.execute("UPDATE app.convites SET visto_em = coalesce(visto_em, now()) WHERE id = %s", (c["id"],))
    checks = (c["detalhes"] or {}).get("checks", [])
    return {
        "empresa": c["empresa"], "area": c["area"], "municipio": c["municipio"],
        "area_ha": round(c["area_ha"] or 0, 1), "expirado": c["expira_em"] < datetime.now(timezone.utc) and not c["aceito_em"],
        "aceito": bool(c["aceito_em"]),
        "situacao": c["ultimo_status"], "rotulo": regras.ROTULOS.get(c["ultimo_status"], "Em análise"),
        "ocorrencias": [{"titulo": k["titulo"], "status": k["status"], "rotulo": k.get("rotulo") or regras.ROTULOS[k["status"]]}
                        for k in checks if k["status"] in ("bad", "warn") and regras.entra_no_resultado(k)],
        "renovabio": next((k.get("rotulo") for k in checks if k["id"] == "renovabio"), None),
        "protocolo": c["ultimo_protocolo"],
        "consentimento": CONSENTIMENTO,
    }


def aceitar(conn, token: str, nome: str, email: str, telefone: str | None, ip: str | None) -> dict:
    """Registra o aceite e cria (ou atualiza) o produtor no CRM, ligado ao comprador que convidou."""
    c = conn.execute(
        """SELECT c.id, c.expira_em, c.aceito_em, c.empresa_id, e.nome AS empresa, a.nome AS area, a.municipio,
                  a.area_ha, a.ultimo_status
           FROM app.convites c JOIN app.areas a ON a.id = c.area_id JOIN app.empresas e ON e.id = c.empresa_id
           WHERE c.token = %s FOR UPDATE OF c""", (token,)).fetchone()
    if not c:
        raise ValueError("Convite não encontrado.")
    if c["aceito_em"]:
        return {"ja_aceito": True}
    if c["expira_em"] < datetime.now(timezone.utc):
        raise ValueError("Este convite expirou. Peça um novo a quem enviou.")
    nota = (f"Aceitou convite de {c['empresa']} para a área {c['area']} ({c['municipio'] or 'município não informado'}, "
            f"{(c['area_ha'] or 0):.0f} ha, situação: {regras.ROTULOS.get(c['ultimo_status'], 'em análise')}).")
    prospect = conn.execute("SELECT id FROM app.prospects WHERE lower(email) = lower(%s) ORDER BY id LIMIT 1",
                            (email,)).fetchone()
    if prospect:
        pid = prospect["id"]
    else:
        pid = conn.execute(
            """INSERT INTO app.prospects (nome, tipo, categoria, municipio, uf, contato_nome, email, telefone, origem,
                                          etapa, notas)
               VALUES (%s, 'produtor', 'produtor', %s, NULL, %s, %s, %s, 'convite', 'contato', %s) RETURNING id""",
            (nome, c["municipio"], nome, email, telefone, nota)).fetchone()["id"]
    conn.execute("INSERT INTO app.interacoes (prospect_id, tipo, texto) VALUES (%s, 'nota', %s)", (pid, nota))
    conn.execute(
        """UPDATE app.convites SET aceito_em = now(), nome = %s, email = %s, telefone = %s, consentimento_txt = %s,
                                   ip = %s, prospect_id = %s WHERE id = %s""",
        (nome, email, telefone, CONSENTIMENTO, ip, pid, c["id"]))
    return {"ja_aceito": False, "prospect_id": pid}
