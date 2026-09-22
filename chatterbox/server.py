from __future__ import annotations

import io
import inspect
import os
import re
import subprocess
import threading
from pathlib import Path

import torch
import torchaudio
from chatterbox.tts import ChatterboxTTS
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

VOICE_DIR = Path(os.getenv("CHATTERBOX_VOICE_DIR", "/voices"))
REQUESTED_DEVICE = os.getenv("CHATTERBOX_DEVICE", "gpu" if torch.cuda.is_available() else "cpu")
# Accept the deployment-facing "gpu" value while passing PyTorch its CUDA
# device name. Chatterbox ultimately constructs torch devices from this value.
DEVICE = "cuda" if REQUESTED_DEVICE.casefold() == "gpu" else REQUESTED_DEVICE
EXAGGERATION = float(os.getenv("CHATTERBOX_EXAGGERATION", "0.5"))
CFG_WEIGHT = float(os.getenv("CHATTERBOX_CFG_WEIGHT", "0.5"))

app = FastAPI(title="Patch Notes Chatterbox TTS")
_model: ChatterboxTTS | None = None
_model_lock = threading.Lock()


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=5000)
    model: str = "chatterbox"
    voice: str = "default"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: str = "wav"
    exaggeration: float = Field(default=EXAGGERATION, ge=0.0, le=2.0)
    cfg_weight: float = Field(default=CFG_WEIGHT, ge=0.0, le=1.0)
    temperature: float = Field(default=0.8, ge=0.05, le=5.0)
    min_p: float = Field(default=0.05, ge=0.0, le=1.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    repetition_penalty: float = Field(default=1.2, ge=0.0, le=2.0)


def get_model() -> ChatterboxTTS:
    global _model
    with _model_lock:
        if _model is None:
            _model = ChatterboxTTS.from_pretrained(device=DEVICE)
    return _model


def voice_prompt(voice: str) -> str | None:
    if voice == "default":
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", voice):
        raise HTTPException(status_code=422, detail="Voice must be a filename-safe voice ID")
    host_match = re.fullmatch(r"host-([a-f0-9]{32})", voice)
    path = VOICE_DIR / host_match.group(1) / "reference.wav" if host_match else VOICE_DIR / f"{voice}.wav"
    if not path.is_file():
        raise HTTPException(
            status_code=422,
            detail=f"Voice '{voice}' is missing; add {voice}.wav to the voices directory",
        )
    return str(path)


def available_voices() -> list[dict[str, str | bool]]:
    """Return the built-in voice and every usable reference WAV."""
    voices: list[dict[str, str | bool]] = [
        {"id": "default", "label": "Default (built in)", "reference": False}
    ]
    if not VOICE_DIR.is_dir():
        return voices

    references = sorted(VOICE_DIR.glob("*.wav"), key=lambda path: path.stem.casefold())
    for path in references:
        voice_id = path.stem
        if voice_id == "default" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", voice_id):
            continue
        voices.append({"id": voice_id, "label": voice_id.replace("_", " ").replace("-", " ").title(), "reference": True})
    return voices


def encode_wav(wav: torch.Tensor, sample_rate: int, speed: float) -> bytes:
    buffer = io.BytesIO()
    torchaudio.save(buffer, wav.cpu(), sample_rate, format="wav")
    audio = buffer.getvalue()
    if speed == 1.0:
        return audio
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
            "-filter:a", f"atempo={speed}", "-f", "wav", "pipe:1",
        ],
        input=audio,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[:500])
    return result.stdout


def generate_audio(model: ChatterboxTTS, request: SpeechRequest, prompt: str | None) -> torch.Tensor:
    """Apply every supported sampler control across Chatterbox releases.

    Chatterbox 0.1.x exposes the sampling controls on its internal T3 inference
    method, while newer releases may expose them directly on ``generate``.
    """
    options = {
        "exaggeration": request.exaggeration,
        "cfg_weight": request.cfg_weight,
        "temperature": request.temperature,
    }
    advanced = {
        "min_p": request.min_p,
        "top_p": request.top_p,
        "repetition_penalty": request.repetition_penalty,
    }
    generate_parameters = inspect.signature(model.generate).parameters
    if all(field in generate_parameters for field in advanced):
        return model.generate(request.input, audio_prompt_path=prompt, **options, **advanced)

    inference = model.t3.inference

    def configured_inference(*args, **kwargs):
        kwargs.update(advanced)
        return inference(*args, **kwargs)

    model.t3.inference = configured_inference
    try:
        return model.generate(request.input, audio_prompt_path=prompt, **options)
    finally:
        model.t3.inference = inference


@app.get("/health")
def health() -> dict[str, str | bool | None]:
    cuda_available = torch.cuda.is_available()
    device_ready = not DEVICE.casefold().startswith("cuda") or cuda_available
    return {
        "ok": device_ready,
        "provider": "chatterbox",
        "device": DEVICE,
        "cuda_available": cuda_available,
        "gpu": torch.cuda.get_device_name(0) if cuda_available else None,
        "model_loaded": _model is not None,
    }


@app.get("/voices")
def voices() -> dict[str, list[dict[str, str | bool]]]:
    return {"voices": available_voices()}


@app.post("/v1/audio/speech")
def speech(request: SpeechRequest) -> Response:
    if request.model != "chatterbox":
        raise HTTPException(status_code=422, detail="Only the chatterbox model is supported")
    if request.response_format != "wav":
        raise HTTPException(status_code=422, detail="Only WAV output is supported")

    prompt = voice_prompt(request.voice)
    model = get_model()
    with _model_lock:
        wav = generate_audio(model, request, prompt)
    return Response(encode_wav(wav, model.sr, request.speed), media_type="audio/wav")
