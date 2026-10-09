#!/bin/sh
# Poll the registry and restart VeloCITY when a new image is published.
# Runs in the compose `updater` service (profile "auto-update") with the Docker socket mounted.
# It only ever pulls and recreates the `velocity` service, nothing else on the host.
set -u
INTERVAL=${INTERVAL:-900}
echo "auto-update: checking for a new velocity image every ${INTERVAL}s"
while true; do
  before=$(docker inspect -f '{{.Image}}' velocity 2>/dev/null || true)
  if docker compose pull --quiet velocity; then
    # `up` recreates the container only if the pulled image differs from the running one.
    docker compose up -d --no-build velocity >/dev/null 2>&1
    after=$(docker inspect -f '{{.Image}}' velocity 2>/dev/null || true)
    if [ "$before" != "$after" ]; then
      echo "$(date '+%F %T') updated velocity to ${after#sha256:}"
    fi
  else
    echo "$(date '+%F %T') pull failed; trying again in ${INTERVAL}s"
  fi
  sleep "$INTERVAL"
done
