#!/usr/bin/env bash
# Deploy labslots on this host (run in /opt/labslots, next to docker-compose.yml):
#
#   ./deploy.sh 0.2.0       pull release 0.2.0 from GitHub, switch to it, roll back if unhealthy
#   ./deploy.sh build       build an image from the source in this folder instead (unreleased code)
#   ./deploy.sh rollback    switch back to the image that ran before the last deploy
#   ./deploy.sh status      show the running image, the kept images and container health
#
# Releases are ghcr.io/zamiul-rashid/labslots:<version>; local builds are labslots:<date-time>.
# The running image is kept in .env as LABSLOTS_IMAGE (the compose file reads it), the one before it
# as LABSLOTS_PREV_IMAGE, so a rollback is just pointing the containers back. The last 5 are kept.
# Headscale is not touched: it runs on the host, outside Docker.
set -euo pipefail
cd "$(dirname "$0")"

REPO=ghcr.io/zamiul-rashid/labslots
SERVICES=(labslots labslots-agent backup)
CHECKED=(labslots labslots-agent)          # services with a healthcheck that must pass
KEEP=5

env_get() { grep -oP "^$1=\K.*" .env 2>/dev/null || true; }
current_image() {
  local img; img=$(env_get LABSLOTS_IMAGE)
  [ -n "$img" ] && { echo "$img"; return; }
  img=$(env_get LABSLOTS_TAG)                # written by the deploy.sh before releases
  [ -n "$img" ] && echo "labslots:$img" || echo none
}
set_image() {
  touch .env
  sed -i '/^LABSLOTS_IMAGE=/d; /^LABSLOTS_TAG=/d; /^LABSLOTS_PREV_TAG=/d' .env
  echo "LABSLOTS_IMAGE=$1" >> .env
  if [ -n "${2:-}" ]; then sed -i '/^LABSLOTS_PREV_IMAGE=/d' .env; echo "LABSLOTS_PREV_IMAGE=$2" >> .env; fi
}
health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo missing; }

wait_healthy() {
  local deadline=$((SECONDS + 150))
  while [ $SECONDS -lt $deadline ]; do
    local ok=1
    for c in "${CHECKED[@]}"; do
      case "$(health "$c")" in
        healthy) ;;
        unhealthy|exited|dead|missing) echo "  $c: $(health "$c")"; return 1 ;;
        *) ok=0 ;;
      esac
    done
    [ $ok = 1 ] && return 0
    sleep 5
  done
  for c in "${CHECKED[@]}"; do echo "  $c: $(health "$c")"; done
  return 1
}

switch_to() {
  set_image "$1" "${2:-}"
  LABSLOTS_IMAGE="$1" docker compose up -d --no-build "${SERVICES[@]}"
}

deploy() {
  local new=$1 prev=$2
  switch_to "$new" "$prev"
  echo "waiting for health checks"
  if wait_healthy; then
    echo "deployed $new"
  else
    echo "$new is unhealthy; rolling back to $prev"
    docker logs --tail 30 labslots 2>&1 | sed 's/^/  labslots: /'
    if [ "$prev" = none ]; then echo "nothing to roll back to"; exit 1; fi
    switch_to "$prev"
    wait_healthy && echo "rolled back to $prev" || echo "rollback also unhealthy: check docker logs"
    exit 1
  fi
  # keep the newest images, never the two in use
  docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "^(labslots:[0-9]{8}-[0-9]{6}|$REPO:[0-9]+\.[0-9]+\.[0-9]+)$" |
    tail -n +$((KEEP + 1)) |
    while read -r i; do [ "$i" != "$prev" ] && [ "$i" != "$new" ] && docker rmi "$i" >/dev/null 2>&1 || true; done
}

case "${1:-}" in
  [0-9]*.[0-9]*.[0-9]*)
    prev=$(current_image)
    echo "pulling $REPO:$1 (running: $prev)"
    docker pull -q "$REPO:$1" >/dev/null
    deploy "$REPO:$1" "$prev"
    ;;
  build)
    prev=$(current_image)
    new=labslots:$(date +%Y%m%d-%H%M%S)
    echo "building $new from this folder (running: $prev)"
    docker build -q -t "$new" . >/dev/null
    deploy "$new" "$prev"
    ;;
  rollback)
    prev=$(env_get LABSLOTS_PREV_IMAGE)
    [ -n "$prev" ] || prev=$(env_get LABSLOTS_PREV_TAG | sed 's/^/labslots:/')
    [ -n "$prev" ] || { echo "no previous image recorded"; exit 1; }
    cur=$(current_image)
    echo "rolling back $cur -> $prev"
    switch_to "$prev" "$cur"
    wait_healthy && echo "running $prev"
    ;;
  status)
    echo "running: $(current_image)   previous: $(env_get LABSLOTS_PREV_IMAGE || true)"
    docker images --format '  {{.Repository}}:{{.Tag}}  {{.CreatedSince}}' | grep -E "  (labslots|$REPO):" || true
    for c in labslots labslots-agent labslots-backup ts-scheduler; do echo "  $c: $(health "$c")"; done
    [ -f backups/.last-ok ] && echo "  last backup: $(cat backups/.last-ok)"
    ;;
  *) echo "usage: $0 <version> | build | rollback | status   (versions: https://github.com/Zamiul-rashid/Headscale/releases)"; exit 2 ;;
esac
