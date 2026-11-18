#!/usr/bin/env bash
# Envia a versão commitada do GeoLEM para o servidor e atualiza o sistema.
# Rode no seu computador (Git Bash), na pasta geolem:
#   bash deploy/enviar.sh root@IP_DO_SERVIDOR
# Só vai o que está no git: o .env do servidor, os dados e os backups de lá não são tocados.
set -euo pipefail

DESTINO="${1:?uso: bash deploy/enviar.sh root@IP_DO_SERVIDOR}"
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "Há alterações sem commit. Faça o commit antes de enviar, para saber exatamente o que está no ar." >&2
  exit 1
fi

echo "==> Enviando $(git log -1 --format='%h %s')"
git archive --format=tar HEAD | ssh "$DESTINO" 'mkdir -p /opt/geolem && tar -x -C /opt/geolem'

ssh "$DESTINO" 'cd /opt/geolem
if [ -f .env ]; then
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
  # Cria tabelas e índices novos (o esquema só usa IF NOT EXISTS / OR REPLACE: não apaga nada)
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db     sh -c '"'"'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'"'"' < db/init/01_schema.sql 2>&1 | grep -v NOTICE || true
  echo "Atualizado."
else
  echo "Código enviado. Primeira vez: no servidor, rode  bash /opt/geolem/deploy/instalar.sh SEU_DOMINIO"
fi'
