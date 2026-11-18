#!/usr/bin/env bash
# Grava a conta do MapBiomas Alerta no .env (no seu computador, no Git Bash, ou no servidor):
#   bash deploy/configurar-mapbiomas.sh
# A senha é digitada sem aparecer na tela e não fica no histórico do terminal.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Arquivo .env não encontrado na pasta $(pwd)." >&2; exit 1; }

read -rp "E-mail da conta MapBiomas Alerta: " EMAIL
read -rsp "Senha da conta MapBiomas Alerta: " SENHA; echo
[ -n "$EMAIL" ] && [ -n "$SENHA" ] || { echo "E-mail e senha são obrigatórios." >&2; exit 1; }
case "$SENHA" in *"'"*) echo "A senha não pode ter aspas simples ('). Troque a senha no MapBiomas e rode de novo." >&2; exit 1 ;; esac

# Remove valores antigos e grava os novos (aspas simples: o Compose não interpreta $ dentro delas)
TMP="$(mktemp)"
grep -v -E '^MAPBIOMAS_(EMAIL|SENHA)=' .env > "$TMP" || true
{ cat "$TMP"; printf "MAPBIOMAS_EMAIL=%s\nMAPBIOMAS_SENHA='%s'\n" "$EMAIL" "$SENHA"; } > .env
rm -f "$TMP"
chmod 600 .env 2>/dev/null || true
echo "Conta gravada no .env. Para aplicar: docker compose up -d api worker"
