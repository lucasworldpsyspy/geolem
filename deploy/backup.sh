#!/usr/bin/env bash
# Backup do banco (laudos emitidos, imóveis e bases). Guarda 14 dias em /opt/geolem/backups.
# Os laudos são o que não dá para refazer: copie os backups para fora do servidor de vez em quando
# (veja "Backups" no README).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
chmod 700 backups
ARQUIVO="backups/geolem-$(date +%Y%m%d-%H%M).sql.gz"
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' | gzip > "$ARQUIVO"
find backups -name 'geolem-*.sql.gz' -mtime +14 -delete
echo "$(date '+%F %T') backup ok: $ARQUIVO ($(du -h "$ARQUIVO" | cut -f1))"
