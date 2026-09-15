from __future__ import annotations

import io
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
DEVICE = os.getenv("CHATTERBOX_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
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
    path = VOICE_DIR / f"{voice}.wav"
    if not path.is_file():
        raise HTTPException(
            status_code=422,
            detail=f"Voice '{voice}' is missing; add {voice}.wav to the voices directory",
        )
    return str(path)


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


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"ok": True, "provider": "chatterbox", "device": DEVICE, "model_loaded": _model is not None}


@app.post("/v1/audio/speech")
def speech(request: SpeechRequest) -> Response:
    if request.model != "chatterbox":
        raise HTTPException(status_code=422, detail="Only the chatterbox model is supported")
    if request.response_format != "wav":
        raise HTTPException(status_code=422, detail="Only WAV output is supported")

    prompt = voice_prompt(request.voice)
    model = get_model()
    with _model_lock:
        wav = model.generate(
            request.input,
            audio_prompt_path=prompt,
            exaggeration=EXAGGERATION,
            cfg_weight=CFG_WEIGHT,
        )
    return Response(encode_wav(wav, model.sr, request.speed), media_type="audio/wav")
