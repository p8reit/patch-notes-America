import asyncio

import pytest
from fastapi import HTTPException

from app import main


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
