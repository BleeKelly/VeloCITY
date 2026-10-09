#!/usr/bin/env bash
# (Re)start VeloCITY on Isengard.
#   deploy/deploy.sh               pull the image GitHub Actions published (ghcr.io) and restart
#   deploy/deploy.sh --build       build the image on Isengard from this checkout instead
#   deploy/deploy.sh --seed-data   also copy the local nflverse parquet cache (skips a ~550 MB download)
set -euo pipefail

HOST=${VELOCITY_HOST:-root@192.168.0.218}
APP=${VELOCITY_APPDATA:-/mnt/user/appdata/velocity}
IMAGE=${VELOCITY_IMAGE:-ghcr.io/bleekelly/velocity:latest}
BUILD=false SEED=false
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=true ;;
    --seed-data) SEED=true ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
cd "$(dirname "$0")/.."

ssh "$HOST" "mkdir -p '$APP/app' '$APP/data/raw'"
# Compose file (and the source, for --build). The .env pins the image and data path, so plain
# `docker compose pull && docker compose up -d` in $APP/app works on the server too.
rsync -a --delete \
  --exclude .venv --exclude .pytest_cache --exclude __pycache__ --exclude data --exclude output \
  --exclude appdata --exclude .claude --exclude tests --exclude .git --exclude .github --exclude .ruff_cache \
  --exclude .env \
  ./ "$HOST:$APP/app/"
ssh "$HOST" "printf 'VELOCITY_IMAGE=%s\nVELOCITY_DATA=%s\n' '$( $BUILD && echo velocity:latest || echo "$IMAGE")' '$APP/data' > '$APP/app/.env'"

if $SEED; then
  # Past seasons never change; the current season is re-downloaded by the server anyway.
  rsync -a --ignore-existing --exclude '*.part' data/raw/ "$HOST:$APP/data/raw/"
fi

# The container runs as nobody:users (99:100), Unraid's appdata convention.
if $BUILD; then
  ssh "$HOST" "chown -R 99:100 '$APP/data' && cd '$APP/app' && docker compose up -d --build"
else
  ssh "$HOST" "chown -R 99:100 '$APP/data' && cd '$APP/app' && docker compose pull && docker compose up -d --no-build"
fi
echo "VeloCITY is starting on http://${HOST#*@}:8097 (watch: ssh $HOST docker logs -f velocity)"
