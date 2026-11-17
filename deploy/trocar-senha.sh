#!/usr/bin/env bash
# Troca a senha de acesso ao sistema (rode no servidor):  bash deploy/trocar-senha.sh
set -euo pipefail
cd "$(dirname "$0")/.."
read -rsp "Nova senha: " SENHA; echo
[ ${#SENHA} -ge 10 ] || { echo "Use pelo menos 10 caracteres." >&2; exit 1; }
HASH="$(docker run --rm caddy:2.10 caddy hash-password --plaintext "$SENHA")"
sed -i "s|^ACESSO_SENHA_HASH=.*|ACESSO_SENHA_HASH='$HASH'|" .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d caddy
echo "Senha trocada."
