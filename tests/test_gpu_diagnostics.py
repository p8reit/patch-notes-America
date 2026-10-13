import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app import gpu, main


@pytest.mark.parametrize("service,path,status,label", [
    ("CHATTERBOX_URL", "/models/unload", 403, "Chatterbox"),
    ("LOCAL_IMAGES_URL", "/models/load", 503, "imagegen"),
    ("LOCAL_IMAGES_URL", "/models/unload", 404, "imagegen"),
    ("LOCAL_AI_URL", "/api/generate", 404, "Ollama"),
    ("LOCAL_AI_URL", "/api/ps", 403, "Ollama"),
])
def test_handoff_error_names_service_operation_and_status_without_exposing_secrets(service, path, status, label):
    base = httpx.URL(getattr(main, service))
    request = httpx.Request("POST", base.copy_with(path=path, query=b"key=secret-api-key"),
                           headers={"Authorization": "Bearer secret-control-key"})
    response = httpx.Response(status, request=request, json={"detail": "secret response contents"})
    error = httpx.HTTPStatusError("secret exception contents", request=request, response=response)
    detail = gpu.handoff_failure(error)
    assert f"{label} {path} returned HTTP {status}" in detail
    assert "secret" not in detail
    if status == 403: assert ("API access" if label == "Ollama" else "authorization") in detail
    if status == 503: assert "CUDA-memory" in detail


def test_handoff_distinguishes_services_on_same_hostname_with_different_ports(monkeypatch):
    monkeypatch.setattr(main, "CHATTERBOX_URL", "http://localhost:8000")
    monkeypatch.setattr(main, "LOCAL_IMAGES_URL", "http://localhost:8001/v1/images/generations")
    monkeypatch.setattr(main, "LOCAL_AI_URL", "http://localhost:11434/api/chat")
    request = httpx.Request("POST", "http://localhost:8001/models/load")
    error = httpx.ReadTimeout("secret error", request=request)
    detail = gpu.handoff_failure(error)
    assert "imagegen /models/load timed out" in detail
    assert "secret" not in detail


def test_handoff_keeps_earlier_failure_when_restore_also_fails():
    first = httpx.Request("POST", main.LOCAL_IMAGES_URL.rsplit("/v1/", 1)[0] + "/models/load")
    restore = httpx.Request("POST", main.CHATTERBOX_URL + "/models/load")
    try:
        try:
            httpx.Response(403, request=first).raise_for_status()
        finally:
            httpx.Response(503, request=restore).raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = gpu.handoff_failure(error)
    assert "Chatterbox /models/load returned HTTP 503" in detail
    assert "Earlier failure: imagegen /models/load returned HTTP 403" in detail


def test_handoff_reports_connection_errors_with_no_attached_request():
    detail = gpu.handoff_failure(httpx.ConnectError("secret hostname"))
    assert "model service request could not complete (ConnectError)" in detail
    assert "secret" not in detail


def test_lease_still_restores_speech_and_releases_ownership_after_failed_image_load(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "enabled", True)
    monkeypatch.setattr(gpu, "lock", asyncio.Lock())
    monkeypatch.setattr(gpu, "token_file", tmp_path / "control.key")
    gpu.initialize()
    calls = []
    def respond(request):
        calls.append(str(request.url))
        if request.method == "GET": return httpx.Response(200, json={"models": []})
        if str(request.url).endswith("/models/load") and request.url.host == httpx.URL(main.LOCAL_IMAGES_URL).host:
            return httpx.Response(503, json={"detail": "private model error"})
        return httpx.Response(200, json={})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def run():
        with pytest.raises(HTTPException) as raised:
            async with gpu.lease("image"):
                raise AssertionError("Failed readiness must not reach image inference")
        assert raised.value.status_code == 503
        assert "imagegen /models/load returned HTTP 503" in raised.value.detail
        assert calls[-1] == main.CHATTERBOX_URL + "/models/load"
        assert gpu.owner.get() is None and gpu.state["active"] == "idle"
    asyncio.run(run())


def test_cancelled_image_cleanup_waits_for_inference_before_restoring_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "enabled", True)
    monkeypatch.setattr(gpu, "lock", asyncio.Lock())
    monkeypatch.setattr(gpu, "state", {"active": "idle"})
    monkeypatch.setattr(gpu, "token_file", tmp_path / "control.key")
    monkeypatch.setattr(gpu, "CONTROL_BUSY_RETRY_SECONDS", 0)
    gpu.initialize()
    calls = []
    unloads = 0
    image_url = main.LOCAL_IMAGES_URL.rsplit("/v1/", 1)[0]

    def respond(request):
        nonlocal unloads
        calls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json={"models": []})
        if str(request.url) == image_url + "/models/unload":
            unloads += 1
            if unloads == 1:
                return httpx.Response(409, json={"detail": "Image inference is still active"})
        return httpx.Response(200, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))

    async def run():
        entered = asyncio.Event()

        async def image():
            async with gpu.lease("image"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(image())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls[-3:] == [image_url + "/models/unload", image_url + "/models/unload", main.CHATTERBOX_URL + "/models/load"]
        assert gpu.state["active"] == "idle"
        assert not gpu.lock.locked()
        async with gpu.lease("speech"):
            assert gpu.owner.get() == "speech"

    asyncio.run(run())


def test_permanently_busy_model_is_bounded_and_does_not_restore_competing_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "enabled", True)
    monkeypatch.setattr(gpu, "lock", asyncio.Lock())
    monkeypatch.setattr(gpu, "state", {"active": "idle"})
    monkeypatch.setattr(gpu, "token_file", tmp_path / "control.key")
    monkeypatch.setattr(gpu, "CONTROL_BUSY_TIMEOUT_SECONDS", 0)
    gpu.initialize()
    calls = []
    image_url = main.LOCAL_IMAGES_URL.rsplit("/v1/", 1)[0]

    def respond(request):
        calls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(409 if str(request.url) == image_url + "/models/unload" else 200, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))

    async def run():
        with pytest.raises(HTTPException) as raised:
            async with gpu.lease("image"):
                pass
        assert raised.value.status_code == 503
        assert "timed out" in raised.value.detail
        assert main.CHATTERBOX_URL + "/models/load" not in calls
        assert gpu.state["active"] == "finishing cancelled inference"
        assert not gpu.lock.locked()

    asyncio.run(run())
