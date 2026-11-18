"""Worker da carteira: processa a fila de análises (app.tarefas) e envia o resumo de alertas por e-mail.

    python -m app.worker             # roda continuamente (serviço "worker" do docker compose)
    python -m app.worker monitorar   # põe todas as carteiras na fila (cron, depois da carga semanal das bases)
    python -m app.worker boletim     # envia o boletim semanal do DETER aos assinantes do observatório
"""
import signal
import sys
import time
import traceback

import psycopg
from psycopg.rows import dict_row

from . import avisos
from .carteira import enfileirar_monitoramento, processar_tarefa
from .config import DATABASE_URL

TENTATIVAS = 3
ESPERA_OCIOSA = 2          # s entre consultas quando a fila está vazia
INTERVALO_AVISOS = 300     # s entre envios do resumo de alertas
parar = False


def _pegar_tarefa(conn) -> dict | None:
    # SKIP LOCKED: vários workers podem rodar juntos sem pegar a mesma tarefa
    linha = conn.execute(
        """UPDATE app.tarefas SET estado = 'executando', iniciado_em = now(), tentativas = tentativas + 1
           WHERE id = (SELECT id FROM app.tarefas WHERE estado = 'pendente' ORDER BY id
                       FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING *""").fetchone()
    conn.commit()
    return linha


def _concluir(conn, tarefa_id: int, estado: str, erro: str | None = None) -> None:
    conn.execute("UPDATE app.tarefas SET estado = %s, erro = %s, concluido_em = now() WHERE id = %s",
                 (estado, erro, tarefa_id))
    conn.commit()


def executar() -> None:
    def encerrar(*_):
        global parar
        parar = True

    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)
    print("Worker da carteira iniciado.", flush=True)
    ultimo_aviso = 0.0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        # Tarefas que ficaram "executando" num desligamento anterior voltam para a fila
        conn.execute("UPDATE app.tarefas SET estado = 'pendente' WHERE estado = 'executando'")
        conn.commit()
        while not parar:
            if time.monotonic() - ultimo_aviso > INTERVALO_AVISOS:
                ultimo_aviso = time.monotonic()
                try:
                    avisos.enviar_resumos(conn)
                except Exception:
                    conn.rollback()
                    traceback.print_exc()
            tarefa = _pegar_tarefa(conn)
            if not tarefa:
                time.sleep(ESPERA_OCIOSA)
                continue
            try:
                protocolo = processar_tarefa(conn, tarefa)
                _concluir(conn, tarefa["id"], "ok")
                print(f"área {tarefa['area_id']}: {protocolo}", flush=True)
            except Exception as e:
                conn.rollback()
                definitivo = tarefa["tentativas"] >= TENTATIVAS or e.__class__.__name__ == "ErroCarteira"
                _concluir(conn, tarefa["id"], "erro" if definitivo else "pendente", str(e)[:500])
                print(f"área {tarefa['area_id']}: falha ({e})", flush=True)
                if not definitivo:
                    time.sleep(5)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "boletim":
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            avisos.enviar_boletins(conn)
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "monitorar":
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            n = enfileirar_monitoramento(conn)
        print(f"{n} área(s) na fila para reanálise.")
        return 0
    executar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
