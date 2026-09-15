#!/usr/bin/env bash
set -euo pipefail

readonly app_port="${APP_PORT:-8081}"
readonly app_url="http://127.0.0.1:${app_port}"

show_diagnostics() {
  echo "Container status:" >&2
  docker compose ps app chatterbox >&2 || true
  echo "Recent app and Chatterbox logs:" >&2
  docker compose logs --tail=100 app chatterbox >&2 || true
}

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or is not available on PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: the Docker Compose plugin is not available." >&2
  exit 1
fi

docker_arch=$(docker info --format '{{.Architecture}}' 2>/dev/null || true)
case "${docker_arch}" in
  arm64|aarch64)
    echo "ARM64 Docker host detected; enabling amd64 emulation for Kokoro..."
    if ! docker run --privileged --rm tonistiigi/binfmt --install amd64; then
      echo "Error: Docker could not enable amd64 emulation required by Kokoro." >&2
      echo "Enable x86/amd64 emulation in Docker Desktop, then run this script again." >&2
      exit 1
    fi
    ;;
esac

echo "Building and recreating Patch Notes and Kokoro..."
# Remove services left behind by older Compose definitions. In particular, an
# obsolete Chatterbox container can otherwise keep crash-looping on ARM/LLVM.
docker compose up -d --build --force-recreate --remove-orphans kokoro app

echo "Waiting for ${app_url}/api/health..."
for attempt in {1..30}; do
  if response=$(curl --fail --silent --max-time 3 "${app_url}/api/health" 2>/dev/null) \
    && grep -q '"chatterbox":{"ok":true' <<<"${response}"; then
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
