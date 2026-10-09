#!/bin/sh
# Poll the registry and restart VeloCITY when a new image is published.
# Runs in the compose `updater` service (profile "auto-update") with the Docker socket mounted.
# It only ever pulls and recreates the services in SERVICES (default: velocity), nothing else on the host.
set -u
INTERVAL=${INTERVAL:-900}
SERVICES=${SERVICES:-velocity}
echo "auto-update: checking for a new image for ${SERVICES} every ${INTERVAL}s"
while true; do
  for svc in $SERVICES; do
    before=$(docker inspect -f '{{.Image}}' "$svc" 2>/dev/null || true)
    if docker compose pull --quiet "$svc" >/dev/null 2>&1; then
      # `up` recreates the container only if the pulled image differs from the running one.
      docker compose up -d --no-build "$svc" >/dev/null 2>&1
      after=$(docker inspect -f '{{.Image}}' "$svc" 2>/dev/null || true)
      if [ "$before" != "$after" ]; then
        echo "$(date '+%F %T') updated $svc to ${after#sha256:}"
      fi
    else
      echo "$(date '+%F %T') pull failed for $svc; trying again in ${INTERVAL}s"
    fi
  done
  sleep "$INTERVAL"
done
