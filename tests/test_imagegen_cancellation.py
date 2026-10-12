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
def service(monkeypatch, request):
    # Existing cancellation cases explicitly exercise the Draft profile.
    monkeypatch.setenv("IMAGEGEN_DEFAULT_PRESET", getattr(request, "param", "draft"))
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


@pytest.mark.parametrize('preset,size,steps,guidance', [
    ('draft', (512, 512), 2, 0.0), ('detail', (1024, 1024), 30, 5.0)])
def test_profile_smoke_uses_native_resolution_and_appropriate_inference(service, preset, size, steps, guidance):
    service._loaded_preset = preset
    calls = []
    def pipeline(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(images=[SimpleNamespace(size=(kwargs['width'], kwargs['height']))])
    service._smoke_test(pipeline)
    assert (calls[0]['width'], calls[0]['height']) == size
    assert calls[0]['num_inference_steps'] == steps and calls[0]['guidance_scale'] == guidance
    assert service._readiness['preset'] == preset


@pytest.mark.parametrize('preset,offload', [('draft', False), ('detail', True)])
def test_loading_selects_model_precision_and_memory_strategy(service, monkeypatch, preset, offload):
    calls = []
    class Pipeline:
        _execution_device = SimpleNamespace(type="cuda")
        unet = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device=SimpleNamespace(type='cuda'))]))
        def enable_vae_slicing(self): calls.append('slicing')
        def enable_vae_tiling(self): calls.append('tiling')
        def enable_model_cpu_offload(self): calls.append('offload')
        def to(self, device): calls.append(device)
    def load(model, **kwargs):
        assert service._pipeline is None
        calls.append((model, kwargs))
        return Pipeline()
    monkeypatch.setattr(service, 'AutoPipelineForText2Image', SimpleNamespace(from_pretrained=load))
    monkeypatch.setattr(service.torch, 'float16', 'fp16', raising=False)
    monkeypatch.setattr(service.torch.cuda, 'is_available', lambda: True, raising=False)
    service._pipeline = object()
    service._load_pipeline(preset)
    model, kwargs = calls[0]
    assert model == service.profile(preset)['model']
    assert kwargs['torch_dtype'] == 'fp16' and kwargs['variant'] == 'fp16' and kwargs['use_safetensors']
    assert ('offload' in calls) == offload and ('cuda' in calls) != offload
    assert 'slicing' in calls and 'tiling' in calls
    assert not service._readiness['ok']  # loading alone never establishes readiness


def test_detail_switch_smokes_before_inference_and_reports_real_model(service, monkeypatch):
    calls = []
    def pipeline(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(images=[fake_image()])
    def load(preset):
        assert preset == 'detail'
        service._pipeline = pipeline
        service._loaded_preset = preset
        return pipeline
    monkeypatch.setattr(service, '_load_pipeline', load)
    monkeypatch.setattr(service, '_smoke_test', lambda _: calls.append('smoke'))
    service._pipeline = object()
    result = service._generate_image(service.ImageRequest(prompt='Portrait', preset='detail'), 1152, 768, threading.Event())
    assert calls[0] == 'smoke'
    assert calls[1]['num_inference_steps'] == 30 and calls[1]['guidance_scale'] == 5
    assert result['model'] == service.DETAIL_MODEL_ID and result['width'] == 1152


def test_failed_detail_smoke_blocks_inference_and_readiness(service, monkeypatch):
    calls = []
    service._pipeline = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setattr(service, '_load_pipeline', lambda preset: service._pipeline)
    def failed(_): raise RuntimeError('Smoke failed')
    monkeypatch.setattr(service, '_smoke_test', failed)
    with pytest.raises(HTTPException) as error:
        service._generate_image(service.ImageRequest(prompt='Portrait', preset='detail'), 1024, 1024, threading.Event())
    assert error.value.status_code == 503 and not service._readiness['ok'] and not calls
    assert service._pipeline is None


def test_local_profiles_reject_arbitrary_model_substitution(service, monkeypatch):
    monkeypatch.setattr(service, 'DETAIL_MODEL_ID', 'unsupported/model')
    with pytest.raises(RuntimeError, match='arbitrary model swaps'):
        service.profile('detail')


def test_loading_remains_fail_closed_without_cuda(service, monkeypatch):
    monkeypatch.setattr(service.torch.cuda, 'is_available', lambda: False, raising=False)
    with pytest.raises(RuntimeError, match='CUDA is not visible'):
        service._load_pipeline('detail')


def test_offload_cannot_resolve_to_cpu_execution(service, monkeypatch):
    pipeline = SimpleNamespace(
        enable_vae_slicing=lambda: None, enable_vae_tiling=lambda: None,
        enable_model_cpu_offload=lambda: None, _execution_device=SimpleNamespace(type='cpu'))
    monkeypatch.setattr(service, 'AutoPipelineForText2Image', SimpleNamespace(from_pretrained=lambda *args, **kwargs: pipeline))
    monkeypatch.setattr(service.torch, 'float16', 'fp16', raising=False)
    monkeypatch.setattr(service.torch.cuda, 'is_available', lambda: True, raising=False)
    with pytest.raises(RuntimeError, match='CUDA execution'):
        service._load_pipeline('detail')
    assert service._pipeline is None and not service._readiness['ok']


@pytest.mark.parametrize("service", ["detail"], indirect=True)
def test_service_default_detail_uses_base_settings_without_turbo_defaults(service):
    received = {}
    def pipeline(**kwargs):
        received.update(kwargs)
        return SimpleNamespace(images=[fake_image()])
    class Connection:
        async def is_disconnected(self): return False
    service._pipeline = pipeline
    request = service.ImageRequest(prompt="A photographic portrait")
    assert request.preset == "detail" and request.model == service.DETAIL_MODEL_ID
    result = asyncio.run(service.generate_image(request, Connection()))
    assert received["width"] == 1024 and received["height"] == 1024
    assert received["num_inference_steps"] == 30 and received["guidance_scale"] == 5
    assert result["model"] == service.DETAIL_MODEL_ID
