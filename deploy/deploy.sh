#!/usr/bin/env bash
# (Re)start VeloCITY on a server over SSH.
#   deploy/deploy.sh               pull the image GitHub Actions published (ghcr.io) and restart
#   deploy/deploy.sh --build       build the image on the server from this checkout instead
#   deploy/deploy.sh --seed-data   also copy the local nflverse parquet cache (skips a ~550 MB download)
#
# Server details come from deploy/deploy.env (gitignored) or the environment:
#   VELOCITY_HOST=user@server  VELOCITY_DIR=/srv/velocity  [VELOCITY_USER=uid:gid]  [VELOCITY_IMAGE=...]
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f deploy/deploy.env ]] && source deploy/deploy.env

HOST=${VELOCITY_HOST:?set VELOCITY_HOST in deploy/deploy.env}
APP=${VELOCITY_DIR:?set VELOCITY_DIR in deploy/deploy.env}
IMAGE=${VELOCITY_IMAGE:-ghcr.io/bleekelly/velocity:latest}
RUN_AS=${VELOCITY_USER:-1000:1000}
BUILD=false SEED=false
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=true ;;
    --seed-data) SEED=true ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
$BUILD && IMAGE=velocity:latest

ssh "$HOST" "mkdir -p '$APP/app' '$APP/data/raw'"
# Compose file (and the source, for --build). The .env pins image, data path and user, so plain
# `docker compose pull && docker compose up -d` in $APP/app works on the server too.
rsync -a --delete \
  --exclude .venv --exclude .pytest_cache --exclude __pycache__ --exclude data --exclude output \
  --exclude store --exclude .claude --exclude tests --exclude .git --exclude .github --exclude .ruff_cache \
  --exclude .env --exclude deploy.env \
  ./ "$HOST:$APP/app/"
ssh "$HOST" "printf 'VELOCITY_IMAGE=%s\nVELOCITY_DATA=%s\nVELOCITY_USER=%s\n' '$IMAGE' '$APP/data' '$RUN_AS' > '$APP/app/.env'"

if $SEED; then
  # Past seasons never change; the current season is re-downloaded by the server anyway.
  rsync -a --ignore-existing --exclude '*.part' data/raw/ "$HOST:$APP/data/raw/"
fi

if $BUILD; then
  ssh "$HOST" "chown -R '$RUN_AS' '$APP/data' && cd '$APP/app' && docker compose up -d --build"
else
  ssh "$HOST" "chown -R '$RUN_AS' '$APP/data' && cd '$APP/app' && docker compose pull && docker compose up -d --no-build"
fi
echo "VeloCITY is starting on port 8097 of ${HOST#*@} (logs: ssh $HOST docker logs -f velocity)"
