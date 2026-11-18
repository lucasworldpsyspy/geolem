"""Resumo de alertas da carteira por e-mail para os administradores de cada empresa.

Só envia se SMTP_HOST estiver configurado. O resumo de uma empresa só sai quando a fila dela termina,
para não mandar um e-mail por fornecedor no meio do monitoramento semanal.
"""
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from . import regras
from .config import SMTP, URL_PUBLICA


def montar_email(empresa: str, alertas: list[dict], destinatarios: list[str]) -> EmailMessage:
    n = len(alertas)
    msg = EmailMessage()
    msg["Subject"] = f"GeoLEM: {n} fornecedor(es) mudaram de situação"
    msg["From"] = formataddr(("GeoLEM", SMTP["remetente"]))
    msg["To"] = ", ".join(destinatarios)
    linhas = [f"Olá,", "",
              f"A última verificação da carteira de {empresa} encontrou {n} mudança(s) de situação:", ""]
    for a in alertas:
        linhas.append(f"• {a['nome']}{' (' + a['fornecedor'] + ')' if a['fornecedor'] else ''}: "
                      f"{regras.ROTULOS.get(a['de'], a['de'])} → {regras.ROTULOS.get(a['para'], a['para'])}")
        if a["resumo"]:
            linhas.append(f"  {a['resumo']}")
        linhas.append(f"  Laudo: {URL_PUBLICA}/api/analises/{a['protocolo']}/laudo.pdf")
        linhas.append("")
    linhas += [f"Veja a carteira completa em {URL_PUBLICA}/carteira.html", "",
               "Este é um aviso automático do GeoLEM."]
    msg.set_content("\n".join(linhas))
    return msg


def _enviar(msg: EmailMessage) -> None:
    with smtplib.SMTP(SMTP["host"], SMTP["porta"], timeout=30) as s:
        s.starttls()
        if SMTP["usuario"]:
            s.login(SMTP["usuario"], SMTP["senha"])
        s.send_message(msg)


def montar_boletim(municipio: str, alertas: list[dict], destino: str, token: str) -> EmailMessage:
    n = len(alertas)
    ha = sum(a["area_ha"] or 0 for a in alertas)
    msg = EmailMessage()
    msg["Subject"] = (f"Boletim DETER · {municipio}: {n} alerta(s) de desmatamento na semana" if n
                      else f"Boletim DETER · {municipio}: nenhum alerta novo na semana")
    msg["From"] = formataddr(("GeoLEM", SMTP["remetente"]))
    msg["To"] = destino
    linhas = [f"Boletim semanal de alertas de desmatamento do INPE (DETER Cerrado) em {municipio}.", ""]
    if n:
        linhas.append(f"{n} alerta(s) publicados nos últimos 7 dias, somando {regras.fmt_ha(ha)}:")
        for a in alertas[:20]:
            linhas.append(f"• detectado em {a['data_deteccao']:%d/%m/%Y}: {regras.fmt_ha(a['area_ha'] or 0)}")
        if n > 20:
            linhas.append(f"• e mais {n - 20} alerta(s)")
    else:
        linhas.append("Nenhum alerta novo publicado nos últimos 7 dias.")
    linhas += ["", "Quer saber se algum fornecedor seu está nessas áreas? O GeoLEM monitora a carteira toda semana:",
               f"{URL_PUBLICA}/observatorio.html", "",
               f"Para não receber mais este boletim: {URL_PUBLICA}/api/publico/sair?token={token}"]
    msg.set_content("\n".join(linhas))
    return msg


def enviar_boletins(conn) -> int:
    """Um e-mail por assinante com os alertas DETER publicados na última semana no município dele."""
    if not SMTP["host"]:
        print("Boletim não enviado: SMTP_HOST não configurado.")
        return 0
    from .observatorio import chave_municipio
    assinantes = conn.execute(
        """SELECT DISTINCT ON (lower(email), boletim_municipio) email, boletim_municipio, token_saida
           FROM app.leads WHERE boletim ORDER BY lower(email), boletim_municipio, id DESC""").fetchall()
    alertas = conn.execute(
        """SELECT municipio, data_deteccao, area_ha FROM geo.alertas_desmatamento
           WHERE fonte = 'deter_cerrado' AND data_publicacao >= current_date - 7
           ORDER BY data_deteccao DESC""").fetchall()
    enviados = 0
    for a in assinantes:
        chave = chave_municipio(a["boletim_municipio"])
        do_mun = [x for x in alertas if chave_municipio(x["municipio"]) == chave]
        _enviar(montar_boletim(a["boletim_municipio"], do_mun, a["email"], a["token_saida"]))
        enviados += 1
    print(f"Boletim enviado para {enviados} assinante(s).")
    return enviados


def enviar_resumos(conn) -> int:
    if not SMTP["host"]:
        return 0
    empresas = conn.execute(
        """SELECT DISTINCT al.empresa_id, e.nome FROM app.alertas al JOIN app.empresas e ON e.id = al.empresa_id
           WHERE NOT al.notificado
             AND NOT EXISTS (SELECT 1 FROM app.tarefas t WHERE t.empresa_id = al.empresa_id
                             AND t.estado IN ('pendente', 'executando'))""").fetchall()
    enviados = 0
    for emp in empresas:
        alertas = conn.execute(
            """SELECT al.id, al.de, al.para, al.protocolo, al.resumo, a.nome, a.fornecedor
               FROM app.alertas al JOIN app.areas a ON a.id = al.area_id
               WHERE al.empresa_id = %s AND NOT al.notificado ORDER BY al.id""", (emp["empresa_id"],)).fetchall()
        destinos = [r["email"] for r in conn.execute(
            "SELECT email FROM app.usuarios WHERE empresa_id = %s AND papel = 'admin' AND ativo",
            (emp["empresa_id"],)).fetchall()]
        if alertas and destinos:
            _enviar(montar_email(emp["nome"], alertas, destinos))
            enviados += 1
        conn.execute("UPDATE app.alertas SET notificado = true WHERE id = ANY(%s)", ([a["id"] for a in alertas],))
        conn.commit()
    return enviados
