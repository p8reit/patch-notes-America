from __future__ import annotations

import base64
import io
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import torch
from diffusers import AutoPipelineForText2Image
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DEVICE = os.getenv("IMAGEGEN_DEVICE", "cuda").casefold()
MODEL_ID = os.getenv("IMAGEGEN_MODEL", "stabilityai/sdxl-turbo")
STEPS = int(os.getenv("IMAGEGEN_STEPS", "2"))
MAX_PROMPT_CHARS = int(os.getenv("IMAGEGEN_MAX_PROMPT_CHARS", "1200"))
CPU_OFFLOAD = os.getenv("IMAGEGEN_CPU_OFFLOAD", "true").casefold() in {"1", "true", "yes"}

_pipeline: Any | None = None
_lock = threading.Lock()
_readiness: dict[str, Any] = {"ok": False, "model_loaded": False, "failure": "Model has not loaded"}


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    model: str = MODEL_ID
    size: str = "1024x1024"
    quality: str = "low"
    n: int = Field(default=1, ge=1, le=1)


def _require_cuda() -> None:
    if DEVICE != "cuda":
        raise RuntimeError("Local image generation is fail-closed and requires IMAGEGEN_DEVICE=cuda")
    if not torch.cuda.is_available():
        raise RuntimeError("IMAGEGEN_DEVICE=cuda was requested, but CUDA is not visible inside the container")


def _load_pipeline() -> Any:
    global _pipeline
    _require_cuda()
    pipeline = AutoPipelineForText2Image.from_pretrained(
        MODEL_ID,
        torch_dtype=torch.float16,
        variant="fp16",
        use_safetensors=True,
    )
    pipeline.enable_vae_slicing()
    if CPU_OFFLOAD:
        pipeline.enable_model_cpu_offload()
    else:
        pipeline.to("cuda")
        parameters = list(pipeline.unet.parameters())
        if not parameters or parameters[0].device.type != "cuda":
            raise RuntimeError("Image model did not resolve to CUDA")
    _pipeline = pipeline
    return pipeline


def _smoke_test(pipeline: Any) -> None:
    with torch.inference_mode():
        image = pipeline(
            prompt="A simple amber star on a dark navy background, no text",
            num_inference_steps=1,
            guidance_scale=0.0,
            width=256,
            height=256,
        ).images[0]
    if image.size != (256, 256):
        raise RuntimeError("Image generation smoke test returned an unexpected size")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        pipeline = _load_pipeline()
        _smoke_test(pipeline)
        _readiness.update({
            "ok": True,
            "model_loaded": True,
            "failure": None,
            "model": MODEL_ID,
            "device": "cuda-with-cpu-offload" if CPU_OFFLOAD else str(next(pipeline.unet.parameters()).device),
            "gpu": torch.cuda.get_device_name(0),
        })
    except Exception as exc:
        _readiness.update({"ok": False, "model_loaded": False, "failure": str(exc)})
        raise
    yield


app = FastAPI(title="Patch Notes Local Image Generator", lifespan=lifespan)


@app.get("/health")
async def health():
    return _readiness


@app.post("/v1/images/generations")
def generate_image(request: ImageRequest):
    if not _readiness["ok"] or _pipeline is None:
        raise HTTPException(status_code=503, detail=_readiness["failure"] or "Image model is not ready")
    if request.model != MODEL_ID:
        raise HTTPException(status_code=400, detail=f"Local image model is {MODEL_ID}")
    # SDXL Turbo is most reliable on this VRAM class at these internal sizes;
    # the application crops/scales the result to the requested video aspect.
    requested_aspect = {
        "1024x1536": (512, 768),
        "1024x1024": (512, 512),
        "1536x1024": (768, 512),
    }
    if request.size not in requested_aspect:
        raise HTTPException(status_code=400, detail="Unsupported image size")
    width, height = requested_aspect[request.size]
    try:
        with _lock, torch.inference_mode():
            image = _pipeline(
                prompt=request.prompt,
                num_inference_steps=STEPS,
                guidance_scale=0.0,
                width=width,
                height=height,
            ).images[0]
    except torch.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(status_code=503, detail="GPU ran out of memory during image generation") from exc
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return {
        "created": int(time.time()),
        "data": [{"b64_json": base64.b64encode(output.getvalue()).decode("ascii")}],
        "model": MODEL_ID,
    }
