#!/usr/bin/env bash
# (Re)start VeloCITY on a server over SSH.
#   deploy/deploy.sh               pull the image GitHub Actions published (ghcr.io) and restart
#   deploy/deploy.sh --build       build the image on the server from this checkout instead
#   deploy/deploy.sh --seed-data   also copy the local parquet caches (skips a ~550 MB NFL / ~1.5 GB college download)
# Every run also syncs local/overlay/ (if present) to the server's data/overlay/ (and data-ncaa/overlay/).
#
# Server details come from deploy/deploy.env (gitignored) or the environment:
#   VELOCITY_HOST=user@server  VELOCITY_DIR=/srv/velocity  [VELOCITY_USER=uid:gid]  [VELOCITY_IMAGE=...]
#   [VELOCITY_PORT=8097]  [VELOCITY_ADMIN_PORT=8098]  [VELOCITY_ADMIN_PASSWORD=...]
#   [VELOCITY_AUTO_UPDATE=true]  [VELOCITY_UPDATE_INTERVAL=900]   (poll the registry, pull new images)
#   [VELOCITY_NCAA=true]  [VELOCITY_NCAA_PORT=8099]  [VELOCITY_NCAA_ADMIN_PORT=8100]   (the college site too)
#   [VELOCITY_SIBLINGS="NFL=https://...,NCAA=https://..."]   (links between the two sites)
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
# Auto-update follows the published image, so it's off for local builds.
AUTO=${VELOCITY_AUTO_UPDATE:-true}
$BUILD && AUTO=false
NCAA=${VELOCITY_NCAA:-false}
PROFILES=() SERVICES=velocity
[[ "$AUTO" == true ]] && PROFILES+=(auto-update)
[[ "$NCAA" == true ]] && PROFILES+=(ncaa) && SERVICES="velocity velocity-ncaa"

ssh "$HOST" "mkdir -p '$APP/app' '$APP/data/raw'"
[[ "$NCAA" == true ]] && ssh "$HOST" "mkdir -p '$APP/data-ncaa/raw'"
# Compose file (and the source, for --build). The .env pins image, data path and user, so plain
# `docker compose pull && docker compose up -d` in $APP/app works on the server too.
rsync -a --delete \
  --exclude .venv --exclude .pytest_cache --exclude __pycache__ --exclude data --exclude output \
  --exclude store --exclude local --exclude .claude --exclude tests --exclude .git --exclude .github --exclude .ruff_cache \
  --exclude .env --exclude deploy.env \
  ./ "$HOST:$APP/app/"
# Built locally and piped over, so values (like a password) never pass through a remote shell.
{
  printf 'VELOCITY_IMAGE=%s\nVELOCITY_DATA=%s\nVELOCITY_USER=%s\n' "$IMAGE" "$APP/data" "$RUN_AS"
  printf 'VELOCITY_PORT=%s\nVELOCITY_ADMIN_PORT=%s\n' "${VELOCITY_PORT:-8097}" "${VELOCITY_ADMIN_PORT:-8098}"
  printf 'VELOCITY_APP_DIR=%s\nVELOCITY_UPDATE_INTERVAL=%s\n' "$APP/app" "${VELOCITY_UPDATE_INTERVAL:-900}"
  printf 'VELOCITY_UPDATE_SERVICES=%s\n' "$SERVICES"
  printf 'VELOCITY_NCAA_DATA=%s\nVELOCITY_NCAA_PORT=%s\nVELOCITY_NCAA_ADMIN_PORT=%s\n' \
    "$APP/data-ncaa" "${VELOCITY_NCAA_PORT:-8099}" "${VELOCITY_NCAA_ADMIN_PORT:-8100}"
  if (( ${#PROFILES[@]} )); then printf 'COMPOSE_PROFILES=%s\n' "$(IFS=,; echo "${PROFILES[*]}")"; fi
  if [[ -n "${VELOCITY_SIBLINGS:-}" ]]; then printf 'VELOCITY_SIBLINGS=%s\n' "$VELOCITY_SIBLINGS"; fi
  if [[ -n "${VELOCITY_ADMIN_PASSWORD:-}" ]]; then printf 'VELOCITY_ADMIN_PASSWORD=%s\n' "$VELOCITY_ADMIN_PASSWORD"; fi
} | ssh "$HOST" "umask 077 && cat > '$APP/app/.env'"

# Your local overlay (gitignored, never in the image): HTML, scripts and files added to the public site.
DATA_DIRS="'$APP/data'"
[[ "$NCAA" == true ]] && DATA_DIRS+=" '$APP/data-ncaa'"
if [[ -d local/overlay ]]; then
  ssh "$HOST" "mkdir -p '$APP/data/overlay'"
  rsync -a --delete local/overlay/ "$HOST:$APP/data/overlay/"
  if [[ "$NCAA" == true ]]; then
    ssh "$HOST" "mkdir -p '$APP/data-ncaa/overlay'"
    rsync -a --delete local/overlay/ "$HOST:$APP/data-ncaa/overlay/"
  fi
fi

if $SEED; then
  # Past seasons never change; the current season is re-downloaded by the server anyway.
  rsync -a --ignore-existing --exclude '*.part' data/raw/ "$HOST:$APP/data/raw/"
  if [[ "$NCAA" == true && -d data/cfb/raw ]]; then
    rsync -a --ignore-existing --exclude '*.part' data/cfb/raw/ "$HOST:$APP/data-ncaa/raw/"
  fi
fi

if $BUILD; then
  ssh "$HOST" "chown -R '$RUN_AS' $DATA_DIRS && cd '$APP/app' && docker compose up -d --build --remove-orphans"
else
  ssh "$HOST" "chown -R '$RUN_AS' $DATA_DIRS && cd '$APP/app' && docker compose pull && docker compose up -d --no-build --remove-orphans"
fi
echo "VeloCITY is starting: site on port ${VELOCITY_PORT:-8097}, rules admin on port ${VELOCITY_ADMIN_PORT:-8098} of ${HOST#*@}"
[[ "$NCAA" == true ]] && echo "VeloCITY NCAA: site on port ${VELOCITY_NCAA_PORT:-8099}, rules admin on port ${VELOCITY_NCAA_ADMIN_PORT:-8100}"
[[ "$AUTO" == true ]] && echo "Auto-update is on: new images are pulled within ${VELOCITY_UPDATE_INTERVAL:-900}s of a release."
echo "(logs: ssh $HOST docker logs -f velocity)"
