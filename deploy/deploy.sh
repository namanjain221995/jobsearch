#!/usr/bin/env bash
# Release one build on the server. Installed as /opt/jobfinder/deploy.sh and bound to the pipeline's SSH key as a
# forced command, so that key can do nothing else:
#
#   { echo "$REGISTRY_TOKEN"; cat deploy/docker-compose.prod.yml; } | ssh ec2-user@server <commit sha>
#
# Standard input: first line a token that may read the images on ghcr.io, then the compose file.
# The release is healthy when the web app answers /api/health through to the API with the worker online. If it does
# not within three minutes, the previous build is started again and the exit code is 1. Database migrations are not
# undone by that: write migrations the previous build can live with.
set -euo pipefail
cd /opt/jobfinder

tag="${SSH_ORIGINAL_COMMAND:-${1:-}}"
if [[ ! "$tag" =~ ^[0-9a-f]{40}$ ]]; then
  echo "deploy: expected a full commit id, got '${tag:0:60}'" >&2
  exit 2
fi

IFS= read -r token
umask 077
cat > docker-compose.yml.new
if [ -z "$token" ] || [ ! -s docker-compose.yml.new ]; then
  echo "deploy: the registry token or the compose file is missing on standard input" >&2
  exit 2
fi

exec 9> .deploy.lock
if ! flock -n 9; then
  echo "deploy: another release is running" >&2
  exit 1
fi

set_tag() {                       # IMAGE_TAG lives in .env so that plain `docker compose` commands work on the server
  sed -i '/^IMAGE_TAG=/d' .env
  echo "IMAGE_TAG=$1" >> .env
}
healthy() {
  for _ in $(seq 1 90); do
    if curl -fsS --max-time 4 http://127.0.0.1:3010/api/health 2>/dev/null | grep -q '"worker_online": *true'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

previous="$(sed -n 's/^IMAGE_TAG=//p' .env | tail -1)"
echo "deploy: $tag (running now: ${previous:-nothing})"

IMAGE_TAG="$tag" docker compose -f docker-compose.yml.new config -q
printf '%s' "$token" | docker login ghcr.io -u github-actions --password-stdin > /dev/null 2>&1
trap 'docker logout ghcr.io > /dev/null 2>&1 || true' EXIT
IMAGE_TAG="$tag" docker compose -f docker-compose.yml.new pull -q

[ -f docker-compose.yml ] && cp -p docker-compose.yml docker-compose.yml.previous
mv docker-compose.yml.new docker-compose.yml
set_tag "$tag"
docker compose up -d --remove-orphans

if healthy; then
  echo "deploy: $tag is healthy"
  docker image prune -af --filter "until=72h" > /dev/null 2>&1 || true
  exit 0
fi

echo "deploy: $tag did not become healthy" >&2
docker compose ps >&2 || true
docker compose logs --tail 40 migrate backend worker frontend >&2 || true
if [ -n "$previous" ] && [ "$previous" != "$tag" ] && [ -f docker-compose.yml.previous ]; then
  echo "deploy: going back to $previous" >&2
  cp -p docker-compose.yml.previous docker-compose.yml
  set_tag "$previous"
  docker compose up -d --remove-orphans >&2 || true
  healthy && echo "deploy: $previous is running again" >&2 || echo "deploy: $previous is not healthy either" >&2
fi
exit 1
