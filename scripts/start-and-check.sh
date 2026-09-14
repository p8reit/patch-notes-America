#!/usr/bin/env bash
set -euo pipefail

readonly app_port="${APP_PORT:-8081}"
readonly app_url="http://127.0.0.1:${app_port}"

show_diagnostics() {
  echo "Container status:" >&2
  docker compose ps app >&2 || true
  echo "Recent app logs:" >&2
  docker compose logs --tail=100 app >&2 || true
}

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
  if response=$(curl --fail --silent --max-time 3 "${app_url}/api/health" 2>/dev/null); then
    printf '%s\n' "${response}"
    printf '\nApp is available at %s\n' "${app_url}"
    exit 0
  fi

  if [[ -z "$(docker compose ps --status running -q app 2>/dev/null)" ]]; then
    echo "Error: the app container stopped while starting." >&2
    show_diagnostics
    exit 1
  fi

  if (( attempt < 30 )); then
    printf '.'
    sleep 2
  fi
done

printf '\n' >&2
echo "Error: the app did not become reachable on port ${app_port}." >&2
show_diagnostics
exit 1
