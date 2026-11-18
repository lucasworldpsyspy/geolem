"""Licenças: plano, situação, validade e limites de cada empresa cliente.

A empresa operadora (a própria GeoLEM) não tem limites. Os valores dos planos são sugestões iniciais,
editáveis empresa a empresa no admin.
"""
from datetime import date, timedelta

PLANOS = {
    "piloto": {"nome": "Piloto (30 dias)", "limite_areas": 20, "limite_usuarios": 3, "limite_laudos_mes": 30, "dias": 30},
    "essencial": {"nome": "Essencial", "limite_areas": 100, "limite_usuarios": 3, "limite_laudos_mes": 100},
    "profissional": {"nome": "Profissional", "limite_areas": 500, "limite_usuarios": 10, "limite_laudos_mes": 500},
    "corporativo": {"nome": "Corporativo", "limite_areas": None, "limite_usuarios": None, "limite_laudos_mes": None},
    "interno": {"nome": "Interno (GeoLEM)", "limite_areas": None, "limite_usuarios": None, "limite_laudos_mes": None},
}
SITUACOES = {"teste": "Em teste", "ativa": "Ativa", "suspensa": "Suspensa", "cancelada": "Cancelada"}


class ErroLicenca(ValueError):
    pass


def motivo_bloqueio(empresa: dict, hoje: date | None = None) -> str | None:
    """Mensagem para o usuário se a empresa não puder usar o sistema; None se estiver liberada."""
    if empresa.get("operadora"):
        return None
    hoje = hoje or date.today()
    if empresa.get("situacao") in ("suspensa", "cancelada"):
        return "O acesso da sua empresa está suspenso. Fale com a GeoLEM para regularizar."
    if empresa.get("valido_ate") and empresa["valido_ate"] < hoje:
        return ("O período de teste da sua empresa terminou." if empresa.get("situacao") == "teste"
                else "A licença da sua empresa venceu.") + " Fale com a GeoLEM para renovar."
    return None


def _empresa(conn, empresa_id: int) -> dict:
    return conn.execute(
        """SELECT id, operadora, situacao, valido_ate, limite_areas, limite_usuarios, limite_laudos_mes
           FROM app.empresas WHERE id = %s""", (empresa_id,)).fetchone()


def uso(conn, empresa_id: int) -> dict:
    return conn.execute(
        """SELECT (SELECT count(*) FROM app.areas WHERE empresa_id = %(e)s AND ativa) AS areas,
                  (SELECT count(*) FROM app.usuarios WHERE empresa_id = %(e)s AND ativo) AS usuarios,
                  (SELECT count(*) FROM geo.analises WHERE empresa_id = %(e)s AND area_id IS NULL
                     AND criado_em >= date_trunc('month', now())) AS laudos_mes""", {"e": empresa_id}).fetchone()


def vagas(conn, empresa_id: int, recurso: str) -> int | None:
    """Quantos ainda cabem no limite do plano (areas | usuarios | laudos_mes). None = sem limite."""
    e = _empresa(conn, empresa_id)
    limite = e[f"limite_{recurso}"]
    if e["operadora"] or limite is None:
        return None
    return max(limite - uso(conn, empresa_id)[recurso], 0)


NOMES = {"areas": "áreas na carteira", "usuarios": "usuários", "laudos_mes": "laudos avulsos neste mês"}


def exigir(conn, empresa_id: int, recurso: str, quantidade: int = 1) -> None:
    livre = vagas(conn, empresa_id, recurso)
    if livre is not None and livre < quantidade:
        e = _empresa(conn, empresa_id)
        raise ErroLicenca(f"Limite do plano atingido: {e[f'limite_{recurso}']} {NOMES[recurso]}. "
                          "Fale com a GeoLEM para ampliar o plano.")


def aplicar_plano(plano: str, hoje: date | None = None) -> dict:
    """Campos de licença padrão de um plano (usado ao criar empresa ou trocar de plano)."""
    if plano not in PLANOS:
        raise ErroLicenca("Plano inválido.")
    p = PLANOS[plano]
    campos = {k: p[k] for k in ("limite_areas", "limite_usuarios", "limite_laudos_mes")}
    if p.get("dias"):
        campos["valido_ate"] = (hoje or date.today()) + timedelta(days=p["dias"])
    return campos
