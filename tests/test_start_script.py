from pathlib import Path


SCRIPT = (Path(__file__).parents[1] / "scripts" / "start-and-check.sh").read_text()


def test_gpu_mode_adds_the_compose_gpu_override():
    assert "--gpu|cuda" in SCRIPT
    assert "compose_files+=(-f docker-compose.gpu.yml)" in SCRIPT
    assert 'docker compose "${compose_files[@]}" "$@"' in SCRIPT
    assert 'requested_device="cuda"' in SCRIPT
    assert 'export CHATTERBOX_DEVICE="${requested_device}"' in SCRIPT


def test_cpu_mode_does_not_implicitly_request_a_gpu():
    assert "--cpu|cpu" in SCRIPT
    assert '${CHATTERBOX_DEVICE:-cpu}' in SCRIPT


def test_local_image_mode_requires_gpu_and_enables_the_profile():
    assert "--local-image" in SCRIPT
    assert "--local-image requires --gpu" in SCRIPT
    assert "--profile local-image" in SCRIPT
    assert "export CLIP_IMAGE_PROVIDER=local" in SCRIPT


def test_startup_rejects_a_running_service_with_the_wrong_device():
    assert '[[ "${resolved_request}" != "${requested_device}" ]]' in SCRIPT
    assert "stale or different Compose deployment" in SCRIPT
