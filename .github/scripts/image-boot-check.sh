#!/usr/bin/env bash
# Boots the built image against the compose Postgres + PgBouncer: runs the migration entrypoint,
# starts the API, and requires /ready = 200 (databases reachable, events off). Usage: image-boot-check.sh <image>
set -euo pipefail
img=${1:?usage: image-boot-check.sh <image>}
net=${COMPOSE_NETWORK:-llm-retrieval-generation_default}
db='postgresql://rag_admin:devpassword@postgres:5432'
app='postgresql://rag_app_login:devpassword@pgbouncer:5432'
env_args=(
  -e APP_ENV=dev -e PG_BOUNCER_MODE=true -e EVENTS_ENABLED=false -e TEMPORAL_ADDRESS=
  -e CORPUS_DATABASE_URL="$app/llm_corpus" -e GENERATION_DATABASE_URL="$app/llm_generation"
  -e CORPUS_MIGRATION_DATABASE_URL="$db/llm_corpus" -e GENERATION_MIGRATION_DATABASE_URL="$db/llm_generation"
  -e VALKEY_URL=redis://redis:6379/0 -e QDRANT_URL=http://qdrant:6333
)
cleanup() { docker rm -f boot-check >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run --rm --network "$net" "${env_args[@]}" --entrypoint python "$img" -m rag.infrastructure.persistence.migrate
docker run -d --name boot-check --network "$net" "${env_args[@]}" "$img" >/dev/null
for _ in $(seq 1 30); do
  code=$(docker exec boot-check python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/ready', timeout=2).status)" 2>/dev/null || true)
  [ "$code" = 200 ] && { echo "image booted: /ready 200"; exit 0; }
  sleep 2
done
docker logs boot-check | tail -40
echo "::error::/ready never returned 200"; exit 1
