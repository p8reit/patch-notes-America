#!/usr/bin/env bash
set -euo pipefail

readonly app_url="http://localhost:8081"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or is not available on PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: the Docker Compose plugin is not available." >&2
  exit 1
fi

echo "Building and recreating the app container..."
docker compose up -d --build --force-recreate app

echo "Waiting for ${app_url}/api/health..."
for attempt in {1..30}; do
  if curl --fail --silent --show-error "${app_url}/api/health"; then
    printf '\nApp is available at %s\n' "${app_url}"
    exit 0
  fi

  if (( attempt < 30 )); then
    sleep 2
  fi
done

echo "Error: the app did not become reachable on port 8081." >&2
docker compose ps app >&2 || true
docker compose logs --tail=100 app >&2 || true
exit 1
