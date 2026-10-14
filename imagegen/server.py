from __future__ import annotations

import asyncio
import gc
import hmac
import base64
import io
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import torch
from diffusers import AutoPipelineForText2Image
from fastapi import FastAPI, HTTPException, Request, Header
from pathlib import Path
from pydantic import BaseModel, Field

DEVICE = os.getenv("IMAGEGEN_DEVICE", "cuda").casefold()
MODEL_ID = os.getenv("IMAGEGEN_MODEL", "stabilityai/sdxl-turbo")
MODEL_REVISION = os.getenv("IMAGEGEN_MODEL_REVISION", "")
STEPS = int(os.getenv("IMAGEGEN_STEPS", "2"))
DETAIL_MODEL_ID = os.getenv("IMAGEGEN_DETAIL_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")
DETAIL_MODEL_REVISION = os.getenv("IMAGEGEN_DETAIL_MODEL_REVISION", "")
DETAIL_STEPS = int(os.getenv("IMAGEGEN_DETAIL_STEPS", "30"))
DETAIL_GUIDANCE = float(os.getenv("IMAGEGEN_DETAIL_GUIDANCE", "5.0"))
DETAIL_CPU_OFFLOAD = os.getenv("IMAGEGEN_DETAIL_CPU_OFFLOAD", "true").lower() in {"1", "true", "yes"}
DEFAULT_PRESET = os.getenv("IMAGEGEN_DEFAULT_PRESET", "detail")
_loaded_preset = DEFAULT_PRESET


def profile(preset: str) -> dict[str, Any]:
    if preset == "draft":
        if MODEL_ID != "stabilityai/sdxl-turbo" or not 1 <= STEPS <= 4:
            raise RuntimeError("Draft requires SDXL Turbo and 1–4 inference steps")
        return {"model": MODEL_ID, "revision": MODEL_REVISION, "steps": STEPS,
                "guidance": 0.0, "offload": CPU_OFFLOAD,
                "sizes": {"1024x1024": (512, 512), "1536x1024": (768, 512), "1024x1536": (512, 768)}}
    if preset != "detail":
        raise RuntimeError("Image preset must be draft or detail")
    if DETAIL_MODEL_ID != "stabilityai/stable-diffusion-xl-base-1.0":
        raise RuntimeError("Detail requires the supported SDXL Base model; arbitrary model swaps are not supported")
    if not 20 <= DETAIL_STEPS <= 50 or not 1 <= DETAIL_GUIDANCE <= 10:
        raise RuntimeError("SDXL Detail requires 20–50 steps and guidance between 1 and 10")
    return {"model": DETAIL_MODEL_ID, "revision": DETAIL_MODEL_REVISION, "steps": DETAIL_STEPS,
            "guidance": DETAIL_GUIDANCE, "offload": DETAIL_CPU_OFFLOAD,
            "sizes": {"1024x1024": (1024, 1024), "1536x1024": (1152, 768), "1024x1536": (768, 1152)}}
# The application adds editorial-safety instructions to as many as 2,000
# characters of producer direction. Keep the service limit large enough for
# that complete, application-generated prompt instead of rejecting it at the
# request-validation boundary.
MAX_PROMPT_CHARS = int(os.getenv("IMAGEGEN_MAX_PROMPT_CHARS", "4000"))
CPU_OFFLOAD = os.getenv("IMAGEGEN_CPU_OFFLOAD", "false").casefold() in {"1", "true", "yes"}

_pipeline: Any | None = None
_lock = threading.Lock()
_readiness: dict[str, Any] = {"ok": False, "model_loaded": False, "failure": "Model has not loaded"}


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    model: str = DETAIL_MODEL_ID if DEFAULT_PRESET == "detail" else MODEL_ID
    size: str = "1024x1024"
    quality: Literal["low", "medium", "high"] = "high"
    n: int = Field(default=1, ge=1, le=1)
    seed: int | None = Field(default=None,ge=0,le=2147483647)
    preset: Literal["draft","detail"] = DEFAULT_PRESET


def _require_cuda() -> None:
    if DEVICE != "cuda":
        raise RuntimeError("Local image generation is fail-closed and requires IMAGEGEN_DEVICE=cuda")
    if not torch.cuda.is_available():
        raise RuntimeError("IMAGEGEN_DEVICE=cuda was requested, but CUDA is not visible inside the container")


def _release_pipeline() -> None:
    """Remove offload hooks and release model references before collecting memory."""
    global _pipeline
    pipeline, _pipeline = _pipeline, None
    _readiness.update(ok=False, model_loaded=False)
    try:
        remove_hooks = getattr(pipeline, "remove_all_hooks", None)
        if callable(remove_hooks):
            remove_hooks()
    finally:
        del pipeline
        gc.collect()
        torch.cuda.empty_cache()


def _load_pipeline(preset: str | None = None) -> Any:
    global _pipeline, _loaded_preset
    _require_cuda()
    preset = preset or _loaded_preset
    settings = profile(preset)
    # Never hold Turbo and Base weights at the same time, even on a failed load.
    _release_pipeline()
    _readiness.update(parked=False, failure="Loading and testing image model")
    pipeline = AutoPipelineForText2Image.from_pretrained(
        settings["model"],
        torch_dtype=torch.float16,
        variant="fp16",
        use_safetensors=True,
        revision=settings["revision"] or None,
    )
    pipeline.enable_vae_slicing()
    pipeline.enable_vae_tiling()
    if settings["offload"]:
        pipeline.enable_model_cpu_offload()
        if pipeline._execution_device.type != "cuda":
            raise RuntimeError("Offloaded image model did not resolve to CUDA execution")
    else:
        pipeline.to("cuda")
        parameters = list(pipeline.unet.parameters())
        if not parameters or parameters[0].device.type != "cuda":
            raise RuntimeError("Image model did not resolve to CUDA")
    _loaded_preset = preset
    _pipeline = pipeline
    return pipeline


def _smoke_test(pipeline: Any) -> None:
    settings = profile(_loaded_preset)
    width, height = settings["sizes"]["1024x1024"]
    # Exercise native resolution, VAE decode and model-appropriate CFG/steps.
    with torch.inference_mode():
        image = pipeline(
            prompt="A fictional adult in soft window light, natural skin texture, no text",
            num_inference_steps=settings["steps"], guidance_scale=settings["guidance"],
            width=width, height=height,
        ).images[0]
    if image.size != (width, height):
        raise RuntimeError("Image generation smoke test returned an unexpected size")
    _readiness.update(model=settings["model"], preset=_loaded_preset,
                      steps=settings["steps"], guidance=settings["guidance"],
                      smoke_size=[width, height], cpu_offload=settings["offload"])


def _prepare_pipeline(preset: str | None = None) -> None:
    """Release temporary references even when CUDA smoke validation fails."""
    try:
        pipeline = _load_pipeline() if preset is None else _load_pipeline(preset)
        try:
            _smoke_test(pipeline)
        finally:
            del pipeline
    except Exception:
        _release_pipeline()
        raise
    _readiness.update(ok=True, model_loaded=True, parked=False, failure=None,
                      device="cuda-with-cpu-offload" if profile(_loaded_preset)["offload"] else "cuda")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("IMAGEGEN_LAZY_LOAD", "false").lower() == "true":
        _require_cuda()
        _readiness.update(ok=False, model_loaded=False, parked=True, cuda_visible=True,
                          failure="Image model parked until a GPU lease requests it")
        yield
        return
    try:
        pipeline = _load_pipeline()
        _smoke_test(pipeline)
        _readiness.update({
            "ok": True,
            "parked": False,
            "model_loaded": True,
            "failure": None,
            "model": profile(_loaded_preset)["model"],
            "device": "cuda-with-cpu-offload" if profile(_loaded_preset)["offload"] else str(next(pipeline.unet.parameters()).device),
            "gpu": torch.cuda.get_device_name(0),
        })
        del pipeline  # Do not retain startup weights across later preset switches.
    except Exception as exc:
        _readiness.update({"ok": False, "model_loaded": False, "failure": str(exc)})
        raise
    yield


app = FastAPI(title="Patch Notes Local Image Generator", lifespan=lifespan)


def authorize_control(authorization: str) -> None:
    path = Path(os.getenv("MODEL_CONTROL_FILE", "/control/model-control.key"))
    if not path.is_file() or not hmac.compare_digest(authorization, "Bearer " + path.read_text().strip()):
        raise HTTPException(403, "Model control is not authorized")


@app.post("/models/unload")
def unload_model(authorization: str = Header("")):
    global _pipeline
    authorize_control(authorization)
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Image inference is still active")
    try:
        _release_pipeline()
        _readiness.update(parked=True, failure="Image model parked")
        return _readiness
    finally:
        _lock.release()


class ModelLoadRequest(BaseModel):
    preset: Literal["draft", "detail"]


@app.post("/models/load")
def load_model(authorization: str = Header(""), request: ModelLoadRequest | None = None):
    authorize_control(authorization)
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Image model is busy")
    try:
        preset = request.preset if request is not None else None
        if not _readiness["ok"] or _pipeline is None or (preset is not None and preset != _loaded_preset):
            _prepare_pipeline(preset)
        free, total = torch.cuda.mem_get_info()
        return {**_readiness, "free_vram_mb": free // 1048576, "total_vram_mb": total // 1048576}
    except Exception as exc:
        _readiness.update(ok=False, failure=str(exc))
        raise HTTPException(503, "Local image model failed CUDA readiness") from exc
    finally:
        _lock.release()


@app.get("/health")
async def health():
    return _readiness


@app.post("/v1/images/generations")
async def generate_image(request: ImageRequest, connection: Request):
    if not _readiness["ok"] or _pipeline is None:
        raise HTTPException(status_code=503, detail=_readiness["failure"] or "Image model is not ready")
    settings = profile(request.preset)
    if request.model not in {MODEL_ID, settings["model"]}:
        raise HTTPException(status_code=400, detail="Model does not match the requested local preset")
    if request.size not in settings["sizes"]:
        raise HTTPException(status_code=400, detail="Unsupported image size")
    width, height = settings["sizes"][request.size]
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_generate_image, request, width, height, cancelled))
    try:
        while not task.done():
            if await connection.is_disconnected():
                cancelled.set()
            await asyncio.wait({task}, timeout=0.2)
        return await task
    except ImageGenerationCancelled as exc:
        raise HTTPException(status_code=499, detail="Image generation cancelled") from exc
    finally:
        cancelled.set()
        # A server shutdown can cancel this handler before inference reaches
        # its next checkpoint. Consume the thread task's eventual exception.
        task.add_done_callback(_consume_image_task)


class ImageGenerationCancelled(Exception):
    pass


def _consume_image_task(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def _generate_image(
    request: ImageRequest, width: int, height: int, cancelled: threading.Event,
) -> dict[str, Any]:
    global _pipeline

    def check_cancelled() -> None:
        if cancelled.is_set():
            raise ImageGenerationCancelled()

    def step_finished(_pipeline: Any, _step: int, _timestep: Any, values: dict) -> dict:
        check_cancelled()
        return values

    try:
        with _lock, torch.inference_mode():
            check_cancelled()
            if not _readiness["ok"] or _pipeline is None:
                raise HTTPException(503, "Image model is parked")
            if _loaded_preset != request.preset:
                try:
                    _prepare_pipeline(request.preset)
                except Exception as exc:
                    _readiness.update(ok=False, model_loaded=False, failure="Image preset failed CUDA readiness: " + type(exc).__name__)
                    raise HTTPException(503, "Image preset failed CUDA readiness") from exc
            check_cancelled()
            settings = profile(request.preset)
            _readiness["device"] = "cuda-with-cpu-offload" if settings["offload"] else "cuda"
            image = _pipeline(
                prompt=request.prompt,
                num_inference_steps=settings["steps"],
                guidance_scale=settings["guidance"],
                width=width,
                height=height,
                callback_on_step_end=step_finished,
                **({"generator":torch.Generator(device="cuda").manual_seed(request.seed)} if request.seed is not None else {}),
            ).images[0]
            check_cancelled()
    except torch.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(status_code=503, detail="GPU ran out of memory during image generation") from exc
    check_cancelled()
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return {
        "created": int(time.time()),
        "data": [{"b64_json": base64.b64encode(output.getvalue()).decode("ascii")}],
        "model": settings["model"],
        "preset": request.preset,
        "steps": settings["steps"],
        "guidance": settings["guidance"],
        "width": width, "height": height,
        "model_revision": settings["revision"] or "unrecorded",
        "seed": request.seed,
    }
