from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_compose(name: str) -> dict:
    with (ROOT / name).open(encoding="utf-8") as compose_file:
        return yaml.safe_load(compose_file)


def test_base_compose_intentionally_uses_cpu_without_gpu_requests() -> None:
    chatterbox = load_compose("docker-compose.yml")["services"]["chatterbox"]

    assert chatterbox["environment"]["CHATTERBOX_DEVICE"] == "cpu"
    assert "deploy" not in chatterbox
    assert "gpus" not in chatterbox
    assert "runtime" not in chatterbox
    assert "devices" not in chatterbox


def test_legacy_full_compose_cannot_enable_cuda_without_the_gpu_override() -> None:
    chatterbox = load_compose("docker-compose.full.yml")["services"]["chatterbox"]

    assert chatterbox["environment"]["CHATTERBOX_DEVICE"] == "cpu"


def test_gpu_override_intentionally_requests_one_nvidia_gpu_for_cuda() -> None:
    chatterbox = load_compose("docker-compose.gpu.yml")["services"]["chatterbox"]
    request = chatterbox["deploy"]["resources"]["reservations"]["devices"]

    assert chatterbox["environment"] == {
        "CHATTERBOX_DEVICE": "cuda",
        "NVIDIA_DRIVER_CAPABILITIES": "compute",
    }
    assert request == [{"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}]
    assert "gpus" not in chatterbox
    assert "runtime" not in chatterbox
    assert "devices" not in chatterbox


def test_chatterbox_healthcheck_rejects_an_unready_requested_device() -> None:
    chatterbox = load_compose("docker-compose.yml")["services"]["chatterbox"]
    healthcheck = chatterbox["healthcheck"]

    assert healthcheck["test"][:3] == ["CMD", "python3", "-c"]
    assert "assert data['ok']" in healthcheck["test"][3]
    assert (
        load_compose("docker-compose.yml")["services"]["app"]["depends_on"]
        ["chatterbox"]["condition"]
        == "service_healthy"
    )
