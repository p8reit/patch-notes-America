import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app import gpu, main


def test_episode_submission_gate_surfaces_backend_diagnostic(monkeypatch):
    async def unready():
        return {
            "ok": False,
            "synthesis_ready": False,
            "failure": "CUDA synthesis failed: no kernel image is available",
        }

    monkeypatch.setattr(main, "chatterbox_status", unready)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.require_chatterbox_ready())

    assert raised.value.status_code == 503
    assert "CUDA synthesis failed" in raised.value.detail


def test_episode_submission_gate_accepts_verified_synthesis(monkeypatch):
    expected = {"ok": True, "synthesis_ready": True, "requested_device": "cuda"}

    async def ready():
        return expected

    monkeypatch.setattr(main, "chatterbox_status", ready)

    assert asyncio.run(main.require_chatterbox_ready()) == expected


@pytest.mark.parametrize("failure,busy,load_status,recover", [
    ("Speech model parked while Video Studio uses the GPU", False, 200, True),
    ("Speech model parked while Video Studio uses the GPU", False, 503, False),
    ("Speech model parked while Video Studio uses the GPU", True, 200, False),
    ("CUDA synthesis failed: out of memory", False, 200, False),
])
def test_health_recovers_only_idle_parked_speech(tmp_path, monkeypatch, failure, busy, load_status, recover):
    monkeypatch.setattr(gpu, "enabled", True)
    monkeypatch.setattr(gpu, "lock", asyncio.Lock())
    monkeypatch.setattr(gpu, "state", {"active": "idle"})
    monkeypatch.setattr(gpu, "token_file", tmp_path / "control.key")
    gpu.initialize()
    calls = []
    loaded = False

    def respond(request):
        nonlocal loaded
        calls.append(str(request.url))
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if str(request.url) == main.CHATTERBOX_URL + "/models/load":
            if load_status != 200:
                return httpx.Response(load_status, json={"detail": "Speech model failed synthesis readiness"})
            loaded = True
        return httpx.Response(200, json={"ok": loaded, "synthesis_ready": loaded, "failure": None if loaded else failure})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))

    async def run():
        if busy:
            await gpu.lock.acquire()
        try:
            result = await main.health()
            assert result["chatterbox"]["ok"] is recover
            assert (main.CHATTERBOX_URL + "/models/load" in calls) is (not busy and failure.startswith("Speech model parked"))
        finally:
            if busy:
                gpu.lock.release()

    asyncio.run(run())
