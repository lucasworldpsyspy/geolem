"""Contas: empresas, usuários, senhas, sessões e auditoria.

Senhas: scrypt (biblioteca padrão), com sal aleatório por usuário.
Sessões: token aleatório num cookie HttpOnly; no banco fica só o SHA-256 do token.

Linha de comando (dentro do container da API):
    python -m app.contas nova-empresa "Armazém Exemplo Ltda" --cnpj 00.000.000/0001-00 \
        --admin-nome "Maria Souza" --admin-email maria@exemplo.com.br
    python -m app.contas novo-usuario maria@exemplo.com.br "João Lima" joao@exemplo.com.br [--admin]
    python -m app.contas redefinir-senha joao@exemplo.com.br
    python -m app.contas listar
"""
import argparse
import base64
import hashlib
import hmac
import re
import secrets
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

DURACAO_SESSAO = timedelta(hours=12)       # sem uso por 12 h, precisa entrar de novo
DURACAO_MAXIMA = timedelta(days=7)         # mesmo em uso, renova o login uma vez por semana
SENHA_MINIMA = 10
EMAIL_VALIDO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ErroConta(ValueError):
    pass


# ---------- senhas ----------
def hash_senha(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode(), salt=sal, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(sal).decode() + "$" + base64.b64encode(h).decode()


def conferir_senha(senha: str, guardado: str) -> bool:
    try:
        _, n, r, p, sal, h = guardado.split("$")
        calc = hashlib.scrypt(senha.encode(), salt=base64.b64decode(sal), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(h)))
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def validar_senha(senha: str) -> None:
    if len(senha) < SENHA_MINIMA:
        raise ErroConta(f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")
    if senha.isdigit() or senha.isalpha():
        raise ErroConta("Use letras e números na senha.")


def senha_provisoria() -> str:
    # Sem caracteres ambíguos (0/O, 1/l/I), fácil de ditar por telefone
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        s = "".join(secrets.choice(alfabeto) for _ in range(14))
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            return s


def normalizar_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL_VALIDO.match(e):
        raise ErroConta("E-mail inválido.")
    return e


# ---------- limite de tentativas de login (por IP e por e-mail) ----------
class LimiteTentativas:
    def __init__(self, maximo: int, janela_s: int):
        self.maximo, self.janela = maximo, janela_s
        self.falhas: dict[str, deque] = defaultdict(deque)

    def bloqueado(self, chave: str) -> bool:
        fila = self.falhas[chave]
        agora = time.monotonic()
        while fila and agora - fila[0] > self.janela:
            fila.popleft()
        return len(fila) >= self.maximo

    def falhou(self, chave: str) -> None:
        self.falhas[chave].append(time.monotonic())

    def limpar(self, chave: str) -> None:
        self.falhas.pop(chave, None)


limite_ip = LimiteTentativas(20, 15 * 60)
limite_email = LimiteTentativas(6, 15 * 60)


# ---------- auditoria ----------
def auditar(conn, acao: str, usuario: dict | None = None, alvo: str | None = None, detalhes: dict | None = None,
            ip: str | None = None, empresa_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO app.auditoria (empresa_id, usuario_id, acao, alvo, detalhes, ip) VALUES (%s, %s, %s, %s, %s, %s)",
        (empresa_id or (usuario or {}).get("empresa_id"), (usuario or {}).get("id"), acao, alvo,
         Jsonb(detalhes) if detalhes else None, ip),
    )


# ---------- sessões ----------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


SQL_USUARIO = """SELECT u.id, u.empresa_id, u.nome, u.email, u.papel, u.ativo, u.trocar_senha,
                        e.nome AS empresa_nome, e.ativa AS empresa_ativa
                 FROM app.usuarios u JOIN app.empresas e ON e.id = u.empresa_id"""


def entrar(conn, email: str, senha: str, ip: str | None, agente: str | None) -> tuple[str, dict]:
    """Confere a senha e cria a sessão. Devolve (token, usuário). Mensagem genérica de erro de propósito."""
    try:
        email = normalizar_email(email)
    except ErroConta:
        raise ErroConta("E-mail ou senha incorretos.")
    if limite_ip.bloqueado(ip or "-") or limite_email.bloqueado(email):
        raise ErroConta("Muitas tentativas. Aguarde 15 minutos e tente de novo.")
    linha = conn.execute(
        f"{SQL_USUARIO.replace('SELECT u.id', 'SELECT u.senha_hash, u.id')} WHERE lower(u.email) = %s", (email,)
    ).fetchone()
    if not linha or not conferir_senha(senha, linha["senha_hash"]) or not linha["ativo"] or not linha["empresa_ativa"]:
        limite_ip.falhou(ip or "-")
        limite_email.falhou(email)
        auditar(conn, "login_falhou", None, alvo=email, ip=ip, empresa_id=linha["empresa_id"] if linha else None)
        raise ErroConta("E-mail ou senha incorretos.")
    limite_email.limpar(email)
    token = secrets.token_urlsafe(32)
    agora = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO app.sessoes (token_hash, usuario_id, expira_em, ip, agente) VALUES (%s, %s, %s, %s, %s)",
        (_hash_token(token), linha["id"], agora + DURACAO_SESSAO, ip, (agente or "")[:300]),
    )
    conn.execute("UPDATE app.usuarios SET ultimo_acesso = now() WHERE id = %s", (linha["id"],))
    conn.execute("DELETE FROM app.sessoes WHERE expira_em < now()")
    usuario = {k: v for k, v in linha.items() if k != "senha_hash"}
    auditar(conn, "login", usuario, ip=ip)
    return token, usuario


def usuario_da_sessao(conn, token: str | None) -> dict | None:
    if not token:
        return None
    h = _hash_token(token)
    linha = conn.execute(
        f"""{SQL_USUARIO} JOIN app.sessoes s ON s.usuario_id = u.id
            WHERE s.token_hash = %s AND s.expira_em > now() AND s.criado_em > now() - %s
              AND u.ativo AND e.ativa""",
        (h, DURACAO_MAXIMA),
    ).fetchone()
    if linha:
        # Janela deslizante: cada uso empurra a expiração (no máximo uma escrita por minuto)
        conn.execute("""UPDATE app.sessoes SET visto_em = now(), expira_em = now() + %s
                        WHERE token_hash = %s AND visto_em < now() - interval '1 minute'""", (DURACAO_SESSAO, h))
    return linha


def sair(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM app.sessoes WHERE token_hash = %s", (_hash_token(token),))


def trocar_senha(conn, usuario: dict, atual: str, nova: str, token_atual: str | None) -> None:
    linha = conn.execute("SELECT senha_hash FROM app.usuarios WHERE id = %s", (usuario["id"],)).fetchone()
    if not conferir_senha(atual, linha["senha_hash"]):
        raise ErroConta("A senha atual não confere.")
    if atual == nova:
        raise ErroConta("A nova senha precisa ser diferente da atual.")
    validar_senha(nova)
    conn.execute("UPDATE app.usuarios SET senha_hash = %s, trocar_senha = false WHERE id = %s",
                 (hash_senha(nova), usuario["id"]))
    # Encerra as outras sessões do usuário (ex.: um computador esquecido logado)
    conn.execute("DELETE FROM app.sessoes WHERE usuario_id = %s AND token_hash <> %s",
                 (usuario["id"], _hash_token(token_atual or "")))


# ---------- empresas e usuários ----------
def criar_empresa(conn, nome: str, cnpj: str | None = None) -> int:
    if not (nome or "").strip():
        raise ErroConta("Informe o nome da empresa.")
    return conn.execute("INSERT INTO app.empresas (nome, cnpj) VALUES (%s, %s) RETURNING id",
                        (nome.strip(), (cnpj or "").strip() or None)).fetchone()["id"]


def criar_usuario(conn, empresa_id: int, nome: str, email: str, papel: str = "analista") -> tuple[int, str]:
    """Cria o usuário com senha provisória (troca obrigatória no primeiro acesso). Devolve (id, senha)."""
    email = normalizar_email(email)
    if not (nome or "").strip():
        raise ErroConta("Informe o nome.")
    if papel not in ("admin", "analista"):
        raise ErroConta("Papel inválido.")
    if conn.execute("SELECT 1 FROM app.usuarios WHERE lower(email) = %s", (email,)).fetchone():
        raise ErroConta("Já existe um usuário com esse e-mail.")
    senha = senha_provisoria()
    uid = conn.execute(
        "INSERT INTO app.usuarios (empresa_id, nome, email, senha_hash, papel) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (empresa_id, nome.strip(), email, hash_senha(senha), papel),
    ).fetchone()["id"]
    return uid, senha


def redefinir_senha(conn, usuario_id: int) -> str:
    senha = senha_provisoria()
    conn.execute("UPDATE app.usuarios SET senha_hash = %s, trocar_senha = true WHERE id = %s",
                 (hash_senha(senha), usuario_id))
    conn.execute("DELETE FROM app.sessoes WHERE usuario_id = %s", (usuario_id,))
    return senha


# ---------- linha de comando ----------
def _cli() -> int:
    import psycopg
    from psycopg.rows import dict_row

    from .config import DATABASE_URL

    ap = argparse.ArgumentParser(prog="python -m app.contas", description="Empresas e usuários do GeoLEM.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("nova-empresa", help="cria uma empresa e o primeiro administrador")
    a.add_argument("nome")
    a.add_argument("--cnpj")
    a.add_argument("--admin-nome", required=True)
    a.add_argument("--admin-email", required=True)
    b = sub.add_parser("novo-usuario", help="cria um usuário na empresa de outro usuário")
    b.add_argument("email_da_empresa", help="e-mail de alguém da mesma empresa")
    b.add_argument("nome")
    b.add_argument("email")
    b.add_argument("--admin", action="store_true")
    c = sub.add_parser("redefinir-senha", help="gera uma senha provisória nova")
    c.add_argument("email")
    sub.add_parser("listar", help="lista empresas e usuários")
    args = ap.parse_args()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        try:
            if args.cmd == "nova-empresa":
                eid = criar_empresa(conn, args.nome, args.cnpj)
                uid, senha = criar_usuario(conn, eid, args.admin_nome, args.admin_email, "admin")
                auditar(conn, "empresa_criada", None, alvo=args.nome, empresa_id=eid, detalhes={"via": "linha de comando"})
                print(f"Empresa criada (id {eid}). Administrador: {args.admin_email}")
                print(f"Senha provisória: {senha}   (troca obrigatória no primeiro acesso)")
            elif args.cmd == "novo-usuario":
                ref = conn.execute("SELECT empresa_id FROM app.usuarios WHERE lower(email) = %s",
                                   (normalizar_email(args.email_da_empresa),)).fetchone()
                if not ref:
                    raise ErroConta("Usuário de referência não encontrado.")
                uid, senha = criar_usuario(conn, ref["empresa_id"], args.nome, args.email,
                                           "admin" if args.admin else "analista")
                print(f"Usuário criado: {args.email}\nSenha provisória: {senha}")
            elif args.cmd == "redefinir-senha":
                u = conn.execute("SELECT id FROM app.usuarios WHERE lower(email) = %s",
                                 (normalizar_email(args.email),)).fetchone()
                if not u:
                    raise ErroConta("Usuário não encontrado.")
                print(f"Senha provisória: {redefinir_senha(conn, u['id'])}")
            else:
                for l in conn.execute("""SELECT e.id, e.nome AS empresa, u.email, u.papel, u.ativo, u.ultimo_acesso
                                         FROM app.empresas e LEFT JOIN app.usuarios u ON u.empresa_id = e.id
                                         ORDER BY e.id, u.id""").fetchall():
                    print(f"{l['id']:>3}  {l['empresa']:<35} {l['email'] or '-':<35} {l['papel'] or '':<9} "
                          f"{'ativo' if l['ativo'] else 'inativo' if l['email'] else ''}")
        except ErroConta as e:
            conn.rollback()
            print(f"Erro: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
