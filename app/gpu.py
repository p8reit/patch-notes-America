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


def describe_handoff_error(exc: httpx.HTTPError) -> str:
    """Report service/stage without exposing request credentials or bodies."""
    from app import main
    try:
        request = exc.request
    except RuntimeError:
        request = None
    names = {(url.host, url.port): name for name, url in
             [("Chatterbox", httpx.URL(main.CHATTERBOX_URL)),
              ("imagegen", httpx.URL(main.LOCAL_IMAGES_URL)),
              ("Ollama", httpx.URL(main.LOCAL_AI_URL))]}
    service = names.get((request.url.host, request.url.port), "model service") if request else "model service"
    allowed_paths = {"/models/load", "/models/unload", "/api/ps", "/api/generate"}
    operation = request.url.path if request and request.url.path in allowed_paths else "request"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in {401, 403}:
            hint = ("Ollama API access was denied; check the configured local endpoint and access settings." if service == "Ollama" else
                    "Model-control authorization failed; verify that app, chatterbox and imagegen can read the same shared model-control key.")
        elif code == 404:
            hint = "Check the configured service URL and rebuild outdated containers; for Ollama, also verify the configured model is installed."
        elif code == 503:
            hint = "Check this service's health and logs for model-load, CUDA-memory or smoke-test failures."
        else:
            hint = "Check this service's logs for the underlying error."
        return f"{service} {operation} returned HTTP {code}. {hint}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{service} {operation} timed out. Check service readiness and whether model loading is stalled."
    return f"{service} {operation} could not complete ({type(exc).__name__}). Check that the service is running and reachable from app."


def handoff_failure(exc: httpx.HTTPError) -> str:
    detail = "GPU model handoff failed: " + describe_handoff_error(exc)
    # A failing restore in the existing finally block can replace an earlier
    # handoff error. Surface both without changing cleanup or retry behavior.
    previous = exc.__context__
    seen = {id(exc)}
    while previous is not None and id(previous) not in seen and len(seen) < 4:
        seen.add(id(previous))
        if isinstance(previous, httpx.HTTPError):
            detail += " Earlier failure: " + describe_handoff_error(previous)
            break
        previous = previous.__context__
    return detail


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
            raise HTTPException(503, handoff_failure(exc)) from exc
        finally:
            owner.reset(context)
            state["active"] = "finishing cancelled inference" if state.get("pending") else "idle"
