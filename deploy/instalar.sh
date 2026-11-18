#!/usr/bin/env bash
# Prepara um servidor Ubuntu (22.04 ou 24.04) novo para o GeoLEM e coloca o sistema no ar.
# Rode como root, depois de enviar o código com deploy/enviar.sh:
#   bash /opt/geolem/deploy/instalar.sh laudos.seudominio.com.br voce@seuemail.com.br "Seu Nome"
# O e-mail e o nome são do primeiro administrador (a sua conta), criada na empresa "GeoLEM".
# Pode rodar de novo sem problema: o que já está feito é mantido (inclusive o .env e as senhas).
set -euo pipefail

DOMINIO="${1:?uso: bash deploy/instalar.sh seu.dominio.com.br seu@email.com.br \"Seu Nome\"}"
ADMIN_EMAIL="${2:?informe o e-mail do primeiro administrador}"
ADMIN_NOME="${3:-Administrador}"
cd "$(dirname "$0")/.."
PASTA="$(pwd)"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

echo "==> 1/7 Docker"
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

echo "==> 2/7 Firewall (só SSH, HTTP e HTTPS)"
apt-get install -yq ufw >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> 3/7 Configuração (.env)"
if [ ! -f .env ]; then
  SENHA_DB="$(openssl rand -hex 24)"
  umask 077
  cat > .env <<EOF
POSTGRES_DB=geolem
POSTGRES_USER=geolem
POSTGRES_PASSWORD=$SENHA_DB
REGIAO_BBOX=-46.7,-12.9,-45.0,-11.3
AREA_MINIMA_HA=0.1
DOMINIO=$DOMINIO
IMAGENS_SATELITE=1
# E-mail dos alertas da carteira (opcional): preencha para os administradores receberem o resumo
SMTP_HOST=
SMTP_PORTA=587
SMTP_USUARIO=
SMTP_SENHA=
SMTP_REMETENTE=
EOF
else
  echo "    .env já existe; mantido."
fi

echo "==> 4/7 Subindo banco, API, worker e HTTPS"
$COMPOSE up -d --build
# Tabelas novas em instalações antigas (só cria o que não existe)
$COMPOSE exec -T db sh -c 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/init/01_schema.sql 2>&1 | grep -v NOTICE || true

echo "==> 5/7 Carga das bases públicas (alguns minutos)"
if ! $COMPOSE exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select 1 from geo.cargas where status = '"'"'ok'"'"' limit 1"' | grep -q 1; then
  $COMPOSE exec -T api python -m ingest todas
else
  echo "    Bases já carregadas; mantidas."
fi

echo "==> 6/7 Primeiro administrador"
if $COMPOSE exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select 1 from app.usuarios limit 1"' | grep -q 1; then
  echo "    Já existem usuários; mantidos."
else
  echo
  $COMPOSE exec -T api python -m app.contas nova-empresa "GeoLEM" --admin-nome "$ADMIN_NOME" --admin-email "$ADMIN_EMAIL"
  echo "    Guarde a senha provisória acima: você vai criar a sua senha no primeiro acesso."
  echo
fi

echo "==> 7/7 Tarefas agendadas (bases e monitoramento da carteira toda segunda às 3h, backup diário às 2h)"
chmod +x deploy/*.sh
cat > /etc/cron.d/geolem <<EOF
SHELL=/bin/bash
0 3 * * 1 root cd $PASTA && $COMPOSE exec -T api python -m ingest todas >> /var/log/geolem-bases.log 2>&1; $COMPOSE exec -T api python -m app.worker monitorar >> /var/log/geolem-bases.log 2>&1
0 2 * * * root $PASTA/deploy/backup.sh >> /var/log/geolem-backup.log 2>&1
EOF

echo
echo "Pronto: https://$DOMINIO  (entre com $ADMIN_EMAIL)"
echo "Clientes: crie a empresa e o administrador com"
echo "  cd $PASTA && $COMPOSE exec api python -m app.contas nova-empresa \"Nome do cliente\" --admin-nome \"Nome\" --admin-email email@cliente.com.br"
echo "O certificado HTTPS sai sozinho em alguns segundos, desde que o domínio já aponte para este servidor."
