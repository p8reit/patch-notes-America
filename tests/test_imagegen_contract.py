import re
from pathlib import Path

import yaml

from app.main import MAX_IMAGE_PROMPT_CHARS, clip_art_prompt


ROOT = Path(__file__).parents[1]
SERVER = (ROOT / "imagegen" / "server.py").read_text()


def test_local_image_service_is_fail_closed_on_cuda():
    assert 'DEVICE = os.getenv("IMAGEGEN_DEVICE", "cuda")' in SERVER
    assert "if not torch.cuda.is_available():" in SERVER
    assert "CUDA is not visible inside the container" in SERVER
    assert "_smoke_test(pipeline)" in SERVER


def test_local_image_service_defaults_to_full_cuda_placement():
    assert 'os.getenv("IMAGEGEN_CPU_OFFLOAD", "false")' in SERVER
    assert 'pipeline.enable_model_cpu_offload()' in SERVER
    assert 'pipeline.to("cuda")' in SERVER


def test_local_image_service_has_bounded_openai_compatible_contract():
    assert '@app.post("/v1/images/generations")' in SERVER
    assert "max_length=MAX_PROMPT_CHARS" in SERVER
    assert "n: int = Field(default=1, ge=1, le=1)" in SERVER
    assert '"data": [{"b64_json":' in SERVER


def test_local_image_service_accepts_largest_application_prompt():
    configured_limit = int(
        re.search(r'IMAGEGEN_MAX_PROMPT_CHARS", "(\d+)"', SERVER).group(1)
    )
    largest_prompt = clip_art_prompt(
        "t" * 160,
        "e" * 500,
        "i" * MAX_IMAGE_PROMPT_CHARS,
    )

    assert len(largest_prompt) <= configured_limit


def test_local_image_compose_service_is_profiled_and_not_host_published():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["imagegen"]

    assert service["profiles"] == ["local-image"]
    assert "ports" not in service
    assert service["environment"]["IMAGEGEN_DEVICE"] == "cuda"
    assert service["environment"]["IMAGEGEN_MAX_PROMPT_CHARS"] == "${LOCAL_IMAGE_MAX_PROMPT_CHARS:-4000}"
    assert service["environment"]["IMAGEGEN_CPU_OFFLOAD"] == "${LOCAL_IMAGE_CPU_OFFLOAD:-false}"
    assert "imagegen-models:/models" in service["volumes"]


def test_legacy_full_compose_defaults_to_full_cuda_placement():
    compose = yaml.safe_load((ROOT / "docker-compose.full.yml").read_text())

    assert compose["services"]["imagegen"]["environment"]["IMAGEGEN_CPU_OFFLOAD"] == (
        "${LOCAL_IMAGE_CPU_OFFLOAD:-false}"
    )


def test_gpu_override_reserves_device_for_local_image_service():
    compose = yaml.safe_load((ROOT / "docker-compose.gpu.yml").read_text())
    devices = compose["services"]["imagegen"]["deploy"]["resources"]["reservations"]["devices"]

    assert devices == [{"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}]
