#!/usr/bin/env bash
set -euo pipefail

readonly app_port="${APP_PORT:-8081}"
readonly app_url="http://127.0.0.1:${app_port}"
declare -a compose_files=(-f docker-compose.yml)

usage() {
  cat <<'EOF'
Usage: ./scripts/start-and-check.sh [--cpu|--gpu]

  --cpu  Start the portable CPU deployment (default).
  --gpu  Request an NVIDIA GPU and configure Chatterbox to load on CUDA.

CUDA-capable Torch wheels alone do not grant a container GPU access. The
--gpu mode adds docker-compose.gpu.yml, which requests the device and sets
CHATTERBOX_DEVICE=cuda.
EOF
}

case "${1:-${CHATTERBOX_DEVICE:-cpu}}" in
  --cpu|cpu)
    readonly deployment_mode="CPU"
    readonly requested_device="cpu"
    ;;
  --gpu|cuda)
    readonly deployment_mode="CUDA"
    readonly requested_device="cuda"
    compose_files+=(-f docker-compose.gpu.yml)
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

# Export the selection as well as applying the GPU override. This makes the
# base Compose interpolation and the override agree, and prevents a stale CPU
# value from surviving through an unexpected Compose merge/configuration path.
export CHATTERBOX_DEVICE="${requested_device}"

if (( $# > 1 )); then
  usage >&2
  exit 2
fi

compose() {
  docker compose "${compose_files[@]}" "$@"
}

show_diagnostics() {
  echo "Container status:" >&2
  compose ps app chatterbox >&2 || true
  echo "Recent app and Chatterbox logs:" >&2
  compose logs --tail=100 app chatterbox >&2 || true
}

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or is not available on PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: the Docker Compose plugin is not available." >&2
  exit 1
fi

echo "Building and recreating Patch Notes and Chatterbox in ${deployment_mode} mode..."
compose up -d --build --force-recreate --remove-orphans chatterbox app

echo "Waiting for ${app_url}/api/health..."
for attempt in {1..30}; do
  if response=$(curl --fail --silent --max-time 3 "${app_url}/api/health" 2>/dev/null) \
    && grep -q '"chatterbox":{"ok":true' <<<"${response}"; then
    resolved_request=$(python3 -c \
      'import json,sys; print(json.load(sys.stdin)["chatterbox"]["status"].get("requested_device", ""))' \
      <<<"${response}" 2>/dev/null || true)
    if [[ "${resolved_request}" != "${requested_device}" ]]; then
      echo "Error: requested ${deployment_mode} mode, but the running Chatterbox reports requested_device=${resolved_request:-unknown}." >&2
      echo "The response may be from a stale or different Compose deployment at ${app_url}." >&2
      show_diagnostics
      exit 1
    fi
    printf '%s\n' "${response}"
    printf '\nApp is available at %s\n' "${app_url}"
    exit 0
  fi

  if [[ -z "$(compose ps --status running -q app 2>/dev/null)" ]]; then
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
