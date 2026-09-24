#!/usr/bin/env bash
set -euo pipefail

readonly app_port="${APP_PORT:-8081}"
readonly app_url="http://127.0.0.1:${app_port}"
declare -a compose_files=(-f docker-compose.yml)
declare deployment_mode="CPU"
declare requested_device="${CHATTERBOX_DEVICE:-cpu}"
declare local_image="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/start-and-check.sh [--cpu|--gpu] [--local-image]

  --cpu  Start the portable CPU deployment (default).
  --gpu  Request an NVIDIA GPU and configure Chatterbox to load on CUDA.
  --local-image  Start the CUDA SDXL Turbo image service and use it for clips.

CUDA-capable Torch wheels alone do not grant a container GPU access. The
--gpu mode adds docker-compose.gpu.yml, which requests the device and sets
CHATTERBOX_DEVICE=cuda.
EOF
}

for argument in "$@"; do
  case "${argument}" in
    --cpu|cpu) deployment_mode="CPU"; requested_device="cpu" ;;
    --gpu|cuda) deployment_mode="CUDA"; requested_device="cuda" ;;
    --local-image) local_image="true" ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
case "${requested_device}" in
  cpu) deployment_mode="CPU" ;;
  cuda) deployment_mode="CUDA" ;;
  *) echo "Error: CHATTERBOX_DEVICE must be cpu or cuda." >&2; exit 2 ;;
esac
if [[ "${requested_device}" == "cuda" ]]; then
  compose_files+=(-f docker-compose.gpu.yml)
fi
if [[ "${local_image}" == "true" && "${requested_device}" != "cuda" ]]; then
  echo "Error: --local-image requires --gpu; the image service fails closed without CUDA." >&2
  exit 2
fi

# Export the selection as well as applying the GPU override. This makes the
# base Compose interpolation and the override agree, and prevents a stale CPU
# value from surviving through an unexpected Compose merge/configuration path.
export CHATTERBOX_DEVICE="${requested_device}"
if [[ "${local_image}" == "true" ]]; then
  export CLIP_IMAGE_PROVIDER=local
fi

compose() {
  if [[ "${local_image}" == "true" ]]; then
    docker compose "${compose_files[@]}" --profile local-image "$@"
  else
    docker compose "${compose_files[@]}" "$@"
  fi
}

show_diagnostics() {
  echo "Container status:" >&2
  compose ps app chatterbox imagegen >&2 || true
  echo "Recent app, Chatterbox, and image generator logs:" >&2
  compose logs --tail=100 app chatterbox imagegen >&2 || true
}

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or is not available on PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: the Docker Compose plugin is not available." >&2
  exit 1
fi

declare -a services=(chatterbox app)
if [[ "${local_image}" == "true" ]]; then
  services=(imagegen chatterbox app)
fi
echo "Building and recreating Patch Notes services in ${deployment_mode} mode..."
compose up -d --build --force-recreate --remove-orphans "${services[@]}"

if [[ "${local_image}" == "true" ]]; then
  echo "Waiting for the local image model and CUDA smoke test (the first model download can take several minutes)..."
  image_ready="false"
  for attempt in {1..90}; do
    if compose exec -T imagegen python3 -c \
      "import json,urllib.request; assert json.load(urllib.request.urlopen('http://localhost:8000/health'))['ok']" \
      >/dev/null 2>&1; then
      image_ready="true"
      break
    fi
    if [[ -z "$(compose ps --status running -q imagegen 2>/dev/null)" ]]; then
      echo "Error: the local image generator stopped while loading." >&2
      show_diagnostics
      exit 1
    fi
    printf '.'
    sleep 10
  done
  if [[ "${image_ready}" != "true" ]]; then
    echo "Error: the local image generator did not become ready." >&2
    show_diagnostics
    exit 1
  fi
  printf '\nLocal image generation is ready.\n'
fi

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
