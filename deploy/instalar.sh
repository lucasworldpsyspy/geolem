#!/usr/bin/env bash
# Prepara um servidor Ubuntu (22.04 ou 24.04) novo para o GeoLEM e coloca o sistema no ar.
# Rode como root, depois de enviar o código com deploy/enviar.sh:
#   bash /opt/geolem/deploy/instalar.sh laudos.seudominio.com.br
# Pode rodar de novo sem problema: o que já está feito é mantido (inclusive o .env e as senhas).
set -euo pipefail

DOMINIO="${1:?uso: bash deploy/instalar.sh seu.dominio.com.br}"
cd "$(dirname "$0")/.."
PASTA="$(pwd)"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

echo "==> 1/6 Docker"
if ! command -v docker >/dev/null; then
  apt-get update -q
  apt-get install -yq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

echo "==> 2/6 Firewall (só SSH, HTTP e HTTPS)"
apt-get install -yq ufw >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> 3/6 Configuração (.env)"
if [ ! -f .env ]; then
  SENHA_DB="$(openssl rand -hex 24)"
  SENHA_ACESSO="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)"
  HASH="$(docker run --rm caddy:2.10 caddy hash-password --plaintext "$SENHA_ACESSO")"
  umask 077
  cat > .env <<EOF
POSTGRES_DB=geolem
POSTGRES_USER=geolem
POSTGRES_PASSWORD=$SENHA_DB
REGIAO_BBOX=-46.7,-12.9,-45.0,-11.3
AREA_MINIMA_HA=0.1
DOMINIO=$DOMINIO
ACESSO_USUARIO=geolem
# Hash bcrypt da senha de acesso; as aspas simples impedem o Compose de interpretar os "\$"
ACESSO_SENHA_HASH='$HASH'
EOF
  echo
  echo "    Acesso ao sistema:  usuário geolem  /  senha $SENHA_ACESSO"
  echo "    Guarde essa senha agora: ela não fica salva em texto no servidor."
  echo "    Para trocar depois: bash deploy/trocar-senha.sh"
  echo
else
  echo "    .env já existe; mantido."
fi

echo "==> 4/6 Subindo banco, API e HTTPS"
$COMPOSE up -d --build

echo "==> 5/6 Carga das bases públicas (alguns minutos)"
if ! $COMPOSE exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select 1 from geo.cargas where status = '"'"'ok'"'"' limit 1"' | grep -q 1; then
  $COMPOSE exec -T api python -m ingest todas
else
  echo "    Bases já carregadas; mantidas."
fi

echo "==> 6/6 Tarefas agendadas (bases toda segunda às 3h, backup diário às 2h)"
chmod +x deploy/*.sh
cat > /etc/cron.d/geolem <<EOF
SHELL=/bin/bash
0 3 * * 1 root cd $PASTA && $COMPOSE exec -T api python -m ingest todas >> /var/log/geolem-bases.log 2>&1
0 2 * * * root $PASTA/deploy/backup.sh >> /var/log/geolem-backup.log 2>&1
EOF

echo
echo "Pronto: https://$DOMINIO"
echo "O certificado HTTPS sai sozinho em alguns segundos, desde que o domínio já aponte para este servidor."
