"""Serialize local model ownership and release idle model memory."""
import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

import httpx
from fastapi import HTTPException

enabled = os.getenv("GPU_MODEL_HANDOFF", "false").lower() == "true"
lock = asyncio.Lock()
owner = ContextVar("gpu_owner", default=None)
state = {"active": "idle", "free_vram_mb": None, "total_vram_mb": None}
token_file = Path(os.getenv("MODEL_CONTROL_FILE", Path(__file__).resolve().parent.parent / "data/video/model-control.key"))


def initialize() -> None:
    if enabled and not token_file.exists():
        token_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48))


def model(kind: str, when=None):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if when is not None and not when():
                return await function(*args, **kwargs)
            async with lease(kind):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


async def control(client: httpx.AsyncClient, url: str, operation: str) -> None:
    response = await client.post(url.rstrip("/") + "/models/" + operation,
                                 headers={"Authorization": "Bearer " + token_file.read_text().strip()})
    if response.status_code == 409:
        state["pending"] = True
        raise HTTPException(503, "A model is still finishing cancelled work. Retry after its current step.")
    response.raise_for_status()
    body = response.json()
    for key in ["free_vram_mb", "total_vram_mb"]:
        if key in body:
            state[key] = body[key]


async def unload_planner(client: httpx.AsyncClient) -> None:
    from app import main
    base = main.LOCAL_AI_URL.rsplit("/api/", 1)[0]
    try:
        response = await client.get(base + "/api/ps")
    except httpx.ConnectError:
        return  # Optional Ollama service is not running.
    response.raise_for_status()
    for model in response.json().get("models", []):
        result = await client.post(base + "/api/generate", json={"model": model["name"], "keep_alive": 0})
        result.raise_for_status()


@asynccontextmanager
async def lease(kind: str):
    if not enabled or owner.get() == kind:
        yield
        return
    if owner.get() is not None:
        raise RuntimeError("Cannot acquire another model inside a GPU lease")
    from app import main
    async with lock:
        state["pending"] = False
        state["active"] = kind
        context = owner.set(kind)
        image_url = main.LOCAL_IMAGES_URL.rsplit("/v1/", 1)[0]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
                try:
                    if kind != "speech":
                        await control(client, main.CHATTERBOX_URL, "unload")
                    if kind != "image":
                        try:
                            await control(client, image_url, "unload")
                        except httpx.ConnectError:
                            pass
                    await unload_planner(client)
                    if kind in {"speech", "image"}:
                        await control(client, main.CHATTERBOX_URL if kind == "speech" else image_url, "load")
                    yield
                finally:
                    if kind == "image":
                        await control(client, image_url, "unload")
                    elif kind == "planner":
                        await unload_planner(client)
                    if kind != "speech":
                        await control(client, main.CHATTERBOX_URL, "load")
        except httpx.HTTPError as exc:
            raise HTTPException(503, "GPU model handoff failed. Check local service readiness and shared model-control mounts.") from exc
        finally:
            owner.reset(context)
            state["active"] = "finishing cancelled inference" if state.get("pending") else "idle"
