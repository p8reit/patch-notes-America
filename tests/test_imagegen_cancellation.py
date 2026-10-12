import asyncio
import base64
import importlib.util
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.fixture
def service(monkeypatch):
    # Exercise the real service/cancellation code without weights or CUDA.
    class OutOfMemoryError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        inference_mode=nullcontext, OutOfMemoryError=OutOfMemoryError,
        cuda=SimpleNamespace(empty_cache=lambda: None),
    ))
    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(AutoPipelineForText2Image=object))
    path = Path(__file__).parents[1] / "imagegen/server.py"
    spec = importlib.util.spec_from_file_location("imagegen_cancel_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module._readiness["ok"] = True
    return module


def fake_image():
    return SimpleNamespace(save=lambda output, **_kwargs: output.write(b"\x89PNG\r\n\x1a\nmock"))


def test_cancelled_waiting_request_never_runs_inference(service):
    calls = []
    service._pipeline = lambda **kwargs: calls.append(kwargs)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(service.ImageGenerationCancelled):
        service._generate_image(service.ImageRequest(prompt="Queued request"), 512, 512, cancelled)
    assert not calls
    assert service._lock.acquire(blocking=False)
    service._lock.release()


def test_inference_cancellation_uses_step_callback_and_releases_model_lock(service):
    cancelled = threading.Event()
    steps = []

    def pipeline(**kwargs):
        for step in range(3):
            steps.append(step)
            cancelled.set()
            kwargs["callback_on_step_end"](None, step, None, {})
        pytest.fail("Cancelled inference must stop at the checkpoint")

    service._pipeline = pipeline
    with pytest.raises(service.ImageGenerationCancelled):
        service._generate_image(service.ImageRequest(prompt="Cancel during inference"), 512, 512, cancelled)
    assert steps == [0]
    service._pipeline = lambda **_kwargs: SimpleNamespace(images=[fake_image()])
    result = service._generate_image(service.ImageRequest(prompt="Next request"), 512, 512, threading.Event())
    assert base64.b64decode(result["data"][0]["b64_json"]).startswith(b"\x89PNG")


def test_disconnect_cancels_running_inference_without_blocking_health(service):
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def pipeline(**kwargs):
        started.set()
        assert release.wait(2), "Disconnect monitor must remain responsive during inference"
        try:
            kwargs["callback_on_step_end"](None, 0, None, {})
        finally:
            stopped.set()
        pytest.fail("Disconnected request must not return an image")

    class Connection:
        async def is_disconnected(self):
            if started.is_set():
                # The monitor sets cancellation before the next step returns.
                asyncio.get_running_loop().call_later(0.01, release.set)
                assert (await service.health())["ok"]
                return True
            return False

    service._pipeline = pipeline

    async def run():
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(service.generate_image(
                service.ImageRequest(prompt="Disconnected"), Connection(),
            ), 3)
        assert error.value.status_code == 499

    asyncio.run(run())
    assert stopped.is_set()


def test_connected_request_keeps_openai_compatible_png_response(service):
    received = {}

    def pipeline(**kwargs):
        received.update(kwargs)
        assert kwargs["callback_on_step_end"](None, 0, None, {"latents": "kept"}) == {"latents": "kept"}
        return SimpleNamespace(images=[fake_image()])

    class Connection:
        async def is_disconnected(self):
            return False

    service._pipeline = pipeline
    result = asyncio.run(service.generate_image(service.ImageRequest(prompt="Connected"), Connection()))
    assert result["model"] == service.MODEL_ID
    assert received["width"] == 512 and received["height"] == 512
    assert base64.b64decode(result["data"][0]["b64_json"]).startswith(b"\x89PNG")


def test_model_control_requires_secret_and_refuses_active_inference(service, tmp_path, monkeypatch):
    path = tmp_path / "control.key"
    path.write_text("test-only-secret")
    monkeypatch.setenv("MODEL_CONTROL_FILE", str(path))
    with pytest.raises(HTTPException) as error:
        service.unload_model("Bearer wrong")
    assert error.value.status_code == 403
    service._pipeline = object()
    with service._lock:
        with pytest.raises(HTTPException) as error:
            service.unload_model("Bearer test-only-secret")
        assert error.value.status_code == 409
    service.unload_model("Bearer test-only-secret")
    assert service._pipeline is None and not service._readiness["ok"]


def test_model_reload_cannot_bypass_cuda_smoke_readiness(service, tmp_path, monkeypatch):
    path = tmp_path / "control.key"
    path.write_text("test-only-secret")
    monkeypatch.setenv("MODEL_CONTROL_FILE", str(path))
    service._readiness["ok"] = False
    monkeypatch.setattr(service, "_load_pipeline", lambda: object())
    def failed(_pipeline): raise RuntimeError("CUDA smoke failed")
    monkeypatch.setattr(service, "_smoke_test", failed)
    with pytest.raises(HTTPException) as error:
        service.load_model("Bearer test-only-secret")
    assert error.value.status_code == 503 and not service._readiness["ok"]
