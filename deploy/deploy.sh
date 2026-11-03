#!/usr/bin/env bash
# Выкатка ClipBanner Bot на сервер по SSH-ключу: ровно то, что закоммичено в git (HEAD).
#
#   DEPLOY_HOST=root@1.2.3.4 DEPLOY_KEY=~/.ssh/key ./deploy/deploy.sh
#
# Переменные:
#   DEPLOY_HOST         user@host (обязательно)
#   DEPLOY_KEY          путь к приватному ключу (обязательно)
#   DEPLOY_KNOWN_HOSTS  файл known_hosts с проверенным ключом сервера (по умолчанию ~/.ssh/known_hosts)
#   DEPLOY_DIR          папка на сервере (по умолчанию /opt/uclipper)
#   ENV_FILE            локальный .env, который уедет на сервер ТОЛЬКО если там его ещё нет
#
# На сервере: <DEPLOY_DIR>/src (код, заменяется), <DEPLOY_DIR>/.env и <DEPLOY_DIR>/workspace (остаются).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DEPLOY_HOST:?задайте DEPLOY_HOST=user@host}"
: "${DEPLOY_KEY:?задайте DEPLOY_KEY=путь/к/ключу}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/uclipper}"
KNOWN_HOSTS="${DEPLOY_KNOWN_HOSTS:-$HOME/.ssh/known_hosts}"
ENV_FILE="${ENV_FILE:-.env}"

SSH_OPTS=(-i "$DEPLOY_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15
          -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN_HOSTS")
remote() { ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "$@"; }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Есть незакоммиченные изменения — выкатывается только закоммиченное. Сначала git commit." >&2
  exit 1
fi
REV="$(git rev-parse --short HEAD)"
echo "→ выкатываю $REV на $DEPLOY_HOST:$DEPLOY_DIR"

# 1. Папки и секреты (.env кладём один раз, права 600; потом правится на сервере).
remote "umask 077; mkdir -p '$DEPLOY_DIR/workspace' && chown 10001:10001 '$DEPLOY_DIR/workspace'"
if ! remote "test -f '$DEPLOY_DIR/.env'"; then
  [ -f "$ENV_FILE" ] || { echo "На сервере нет .env, а локального $ENV_FILE тоже нет." >&2; exit 1; }
  echo "→ на сервере нет .env — загружаю $ENV_FILE (chmod 600)"
  ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "umask 077; cat > '$DEPLOY_DIR/.env'" < "$ENV_FILE"
fi

# 2. Код: git archive → src.new → атомарная замена src.
git archive --format=tar HEAD | remote "set -e; rm -rf '$DEPLOY_DIR/src.new'; mkdir -p '$DEPLOY_DIR/src.new'; \
  tar -x -C '$DEPLOY_DIR/src.new'; rm -rf '$DEPLOY_DIR/src.old'; \
  [ -d '$DEPLOY_DIR/src' ] && mv '$DEPLOY_DIR/src' '$DEPLOY_DIR/src.old'; \
  mv '$DEPLOY_DIR/src.new' '$DEPLOY_DIR/src'; echo '$REV' > '$DEPLOY_DIR/src/REVISION'"

# 3. Сборка и запуск (compose сам пересоздаст контейнер, если образ изменился).
remote "cd '$DEPLOY_DIR/src' && docker compose up -d --build 2>&1 | tail -15"

# 4. Проверка: бот должен написать, что запущен.
echo "→ жду запуска бота…"
for _ in $(seq 1 30); do
  if remote "docker logs --tail 50 uclipper 2>&1 | grep -q 'запущен'"; then
    remote "docker logs --tail 12 uclipper 2>&1"
    echo "✓ $REV выкачен и работает"
    exit 0
  fi
  sleep 2
done
echo "✗ бот не сообщил о запуске за минуту. Последние строки журнала:" >&2
remote "docker logs --tail 40 uclipper 2>&1" >&2 || true
exit 1
