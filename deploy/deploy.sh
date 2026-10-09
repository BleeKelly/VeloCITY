#!/usr/bin/env bash
# Build and (re)start VeloCITY on Isengard.
#   deploy/deploy.sh              copy the app, rebuild the image, restart the container
#   deploy/deploy.sh --seed-data  also copy the local nflverse parquet cache (skips a ~550 MB download)
set -euo pipefail

HOST=${VELOCITY_HOST:-root@192.168.0.218}
APP=${VELOCITY_APPDATA:-/mnt/user/appdata/velocity}
cd "$(dirname "$0")/.."

ssh "$HOST" "mkdir -p '$APP/app' '$APP/data/raw'"
rsync -a --delete \
  --exclude .venv --exclude .pytest_cache --exclude __pycache__ --exclude data --exclude output \
  --exclude appdata --exclude .claude --exclude tests --exclude .git --exclude .github --exclude .ruff_cache \
  ./ "$HOST:$APP/app/"

if [[ "${1:-}" == "--seed-data" ]]; then
  # Past seasons never change; the current season is re-downloaded by the server anyway.
  rsync -a --ignore-existing --exclude '*.part' data/raw/ "$HOST:$APP/data/raw/"
fi

# The container runs as nobody:users (99:100), Unraid's appdata convention.
ssh "$HOST" "chown -R 99:100 '$APP/data' && cd '$APP/app' && VELOCITY_DATA='$APP/data' docker compose up -d --build"
echo "VeloCITY is starting on http://${HOST#*@}:8097 (first build scores every season; watch: ssh $HOST docker logs -f velocity)"
