from __future__ import annotations

import io
import importlib.metadata
import inspect
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import torch
import torchaudio
from chatterbox.tts import ChatterboxTTS
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

VOICE_DIR = Path(os.getenv("CHATTERBOX_VOICE_DIR", "/voices"))
# CPU is the known-good default. GPU inference remains available as an explicit
# opt-in with CHATTERBOX_DEVICE=cuda, but is never selected just because a CUDA
# device happens to be visible to the container.
DEVICE = os.getenv("CHATTERBOX_DEVICE", "cpu")
EXAGGERATION = float(os.getenv("CHATTERBOX_EXAGGERATION", "0.5"))
CFG_WEIGHT = float(os.getenv("CHATTERBOX_CFG_WEIGHT", "0.5"))
MAX_INPUT_CHARS = int(os.getenv("CHATTERBOX_MAX_INPUT_CHARS", "300"))
GENERATION_MODE = os.getenv("CHATTERBOX_GENERATION_MODE", "baseline").casefold()

app = FastAPI(title="Patch Notes Chatterbox TTS")
_model: ChatterboxTTS | None = None
_generation_contract: GenerationContract | None = None
_model_lock = threading.Lock()
_readiness: dict[str, Any] = {
    "model_loaded": False,
    "synthesis_ready": False,
    "failure": "Startup smoke synthesis has not completed",
    "model_parameters": [],
    "smoke_audio": None,
}

SMOKE_TEXT = "This is a short Chatterbox readiness test."
MIN_SMOKE_SECONDS = 0.1
MAX_SMOKE_SECONDS = 30.0


class SpeechRequest(BaseModel):
    # Oversized narration can exhaust Chatterbox's acoustic-token window and
    # produce a stuck tone. The application splits prose before this boundary.
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
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
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)


PUBLIC_CONTROLS = ("audio_prompt_path", "exaggeration", "cfg_weight", "temperature")
ADVANCED_CONTROLS = ("min_p", "top_p", "repetition_penalty")


class GenerationContract(NamedTuple):
    """Generation capabilities discovered once, immediately after model loading."""

    chatterbox_version: str
    public_parameters: frozenset[str]
    advanced_adapter: "Chatterbox016SamplerAdapter | None"


class Chatterbox016SamplerAdapter:
    """Private sampler bridge tested only against chatterbox-tts 0.1.6.

    Keeping this version-specific prevents an upstream private signature change
    from silently corrupting audio. Calls are serialized by ``_model_lock``.
    """

    VERSION = "0.1.6"
    REQUIRED_PARAMETERS = frozenset(ADVANCED_CONTROLS)

    def __init__(self, model: Any) -> None:
        inference = getattr(getattr(model, "t3", None), "inference", None)
        if not callable(inference):
            raise RuntimeError("Chatterbox 0.1.6 sampler adapter requires model.t3.inference")
        parameters = inspect.signature(inference).parameters
        missing = self.REQUIRED_PARAMETERS.difference(parameters)
        if missing:
            raise RuntimeError(
                "Chatterbox 0.1.6 sampler signature mismatch; missing: "
                + ", ".join(sorted(missing))
            )

    def generate(
        self,
        model: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        advanced: dict[str, float],
    ) -> torch.Tensor:
        inference = model.t3.inference

        def configured_inference(*inference_args, **inference_kwargs):
            inference_kwargs.update(advanced)
            return inference(*inference_args, **inference_kwargs)

        model.t3.inference = configured_inference
        try:
            return model.generate(*args, **kwargs)
        finally:
            model.t3.inference = inference


def _chatterbox_version() -> str:
    return importlib.metadata.version("chatterbox-tts")


def _build_generation_contract(model: Any, mode: str = GENERATION_MODE) -> GenerationContract:
    if mode not in {"baseline", "advanced"}:
        raise RuntimeError("CHATTERBOX_GENERATION_MODE must be 'baseline' or 'advanced'")
    parameters = frozenset(inspect.signature(model.generate).parameters)
    version = _chatterbox_version()
    adapter = None
    if mode == "advanced" and not set(ADVANCED_CONTROLS).issubset(parameters):
        if version != Chatterbox016SamplerAdapter.VERSION:
            raise RuntimeError(
                f"Advanced sampling has no tested adapter for chatterbox-tts {version}; "
                "use CHATTERBOX_GENERATION_MODE=baseline"
            )
        adapter = Chatterbox016SamplerAdapter(model)
    return GenerationContract(version, parameters, adapter)


def get_model() -> ChatterboxTTS:
    global _model, _generation_contract
    with _model_lock:
        if _model is None:
            _model = ChatterboxTTS.from_pretrained(device=DEVICE)
            _generation_contract = _build_generation_contract(_model)
    return _model


def _representative_parameters(model: Any) -> list[dict[str, str]]:
    """Return a small, truthful sample of the loaded model's parameters."""
    found: list[dict[str, str]] = []
    seen: set[int] = set()
    candidates: Iterable[tuple[str, Any]] = (("model", model),) + tuple(
        (name, getattr(model, name))
        for name in ("t3", "s3gen", "ve")
        if hasattr(model, name)
    )
    for component, module in candidates:
        named_parameters = getattr(module, "named_parameters", None)
        if not callable(named_parameters):
            continue
        for name, parameter in named_parameters():
            if id(parameter) in seen:
                continue
            seen.add(id(parameter))
            found.append({
                "name": f"{component}.{name}" if name else component,
                "device": str(parameter.device),
                "dtype": str(parameter.dtype),
            })
            if len(found) == 3:
                return found
    return found


def _check_model_placement(parameters: list[dict[str, str]], requested_device: str) -> None:
    if not parameters:
        raise RuntimeError("Loaded model exposes no parameters; device placement cannot be verified")
    requested_type = requested_device.casefold().split(":", 1)[0]
    mismatched = [item for item in parameters if item["device"].split(":", 1)[0] != requested_type]
    if mismatched:
        actual = ", ".join(sorted({item["device"] for item in mismatched}))
        raise RuntimeError(f"Model device mismatch: requested {requested_device}, found parameters on {actual}")
    unsupported = [item for item in parameters if item["dtype"] not in {"torch.float16", "torch.float32", "torch.bfloat16"}]
    if unsupported:
        actual = ", ".join(sorted({item["dtype"] for item in unsupported}))
        raise RuntimeError(f"Unsupported model parameter dtype: {actual}")


def _validate_audio(wav: torch.Tensor, sample_rate: int) -> dict[str, float | int]:
    """Reject common silent/corrupt synthesis results before declaring readiness."""
    if not isinstance(wav, torch.Tensor) or wav.numel() == 0:
        raise RuntimeError("Smoke synthesis returned empty audio")
    samples = wav.detach().float().cpu().reshape(-1)
    finite = torch.isfinite(samples)
    non_finite = int((~finite).sum())
    if non_finite:
        raise RuntimeError(f"Smoke synthesis returned {non_finite} non-finite audio samples")
    peak = float(samples.abs().max())
    if peak == 0:
        raise RuntimeError("Smoke synthesis returned all-zero audio")
    clipped_fraction = float((samples.abs() >= 0.999).float().mean())
    rms = float(torch.sqrt(torch.mean(samples.square())))
    dc_offset = float(samples.mean())
    static_fraction = float((samples.abs() >= 0.95).float().mean())
    if static_fraction >= 0.90:
        raise RuntimeError("Smoke synthesis returned near-full-scale static")
    if clipped_fraction > 0.02:
        raise RuntimeError(f"Smoke synthesis is severely clipped ({clipped_fraction:.1%} of samples)")
    duration = samples.numel() / sample_rate
    if not MIN_SMOKE_SECONDS <= duration <= MAX_SMOKE_SECONDS:
        raise RuntimeError(f"Smoke synthesis duration is implausible ({duration:.3f}s)")
    window = max(1, round(sample_rate * 0.1))
    low_run = longest_low_run = 0
    for block in samples.split(window):
        low = block.numel() and float(block.max() - block.min()) <= 0.001 and float(block.abs().max()) >= 0.002
        low_run = low_run + block.numel() if low else 0
        longest_low_run = max(longest_low_run, low_run)
    low_variation_seconds = longest_low_run / sample_rate
    if low_variation_seconds >= 3.0:
        raise RuntimeError("Smoke synthesis contains a multi-second nearly constant tone")

    block_size = max(1, round(sample_rate * 0.5))
    blocks = list(samples.split(block_size))
    repeat_run = repeated_samples = 0
    maximum_similarity = 0.0
    for previous, current in zip(blocks, blocks[1:]):
        if previous.numel() != block_size or current.numel() != block_size:
            continue
        energy = float((previous.square() + current.square()).sum())
        error = float(((previous - current).square()).sum())
        similarity = max(-1.0, 1.0 - 2.0 * error / energy) if energy else 1.0
        maximum_similarity = max(maximum_similarity, similarity)
        if energy and similarity >= 0.9995:
            repeat_run += block_size
            repeated_samples = max(repeated_samples, repeat_run + block_size)
        else:
            repeat_run = 0
    repeated_seconds = repeated_samples / sample_rate
    if repeated_seconds >= 4.0:
        raise RuntimeError("Smoke synthesis contains highly repetitive blocks")
    return {
        "samples": samples.numel(), "duration_seconds": round(duration, 3),
        "peak": round(peak, 6), "rms": round(rms, 6),
        "clipping_ratio": round(clipped_fraction, 8), "non_finite_samples": non_finite,
        "dc_offset": round(dc_offset, 8),
        "long_low_variation_seconds": round(low_variation_seconds, 3),
        "repeated_window_similarity": round(maximum_similarity, 8),
        "repeated_window_seconds": round(repeated_seconds, 3),
    }


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


def generate_audio(
    model: ChatterboxTTS,
    request: SpeechRequest,
    prompt: str | None,
    *,
    mode: str | None = None,
    contract: GenerationContract | None = None,
) -> torch.Tensor:
    """Generate through the public baseline, or an explicitly tested adapter."""
    selected_mode = mode or GENERATION_MODE
    active_contract = contract or _generation_contract
    if active_contract is None:
        raise RuntimeError("Generation contract was not initialized with the model")
    if request.seed is not None:
        torch.manual_seed(request.seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(request.seed)
    public_values = {
        "audio_prompt_path": prompt,
        "exaggeration": request.exaggeration,
        "cfg_weight": request.cfg_weight,
        "temperature": request.temperature,
    }
    options = {key: value for key, value in public_values.items() if key in active_contract.public_parameters}
    advanced = {
        "min_p": request.min_p,
        "top_p": request.top_p,
        "repetition_penalty": request.repetition_penalty,
    }
    if selected_mode == "baseline":
        return model.generate(request.input, **options)
    if selected_mode != "advanced":
        raise RuntimeError(f"Unknown generation mode: {selected_mode}")
    if set(ADVANCED_CONTROLS).issubset(active_contract.public_parameters):
        return model.generate(request.input, **options, **advanced)
    if active_contract.advanced_adapter is None:
        raise RuntimeError("Advanced generation requested without a tested sampler adapter")
    return active_contract.advanced_adapter.generate(model, (request.input,), options, advanced)


def run_startup_smoke_test() -> None:
    """Load the model once and prove that its real inference path produces valid WAV."""
    _readiness.update(model_loaded=False, synthesis_ready=False, failure=None, model_parameters=[], smoke_audio=None)
    try:
        if DEVICE.casefold().startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA was requested but torch.cuda.is_available() is false; check the NVIDIA "
                "driver, container GPU reservation, and CUDA/PyTorch version compatibility"
            )
        model = get_model()
        _readiness["model_loaded"] = True
        parameters = _representative_parameters(model)
        _readiness["model_parameters"] = parameters
        _check_model_placement(parameters, DEVICE)
        request = SpeechRequest(input=SMOKE_TEXT, voice="default")
        with _model_lock:
            generated = generate_audio(model, request, None)
        _validate_audio(generated, model.sr)
        encoded = encode_wav(generated, model.sr, 1.0)
        decoded, decoded_rate = torchaudio.load(io.BytesIO(encoded), format="wav")
        if decoded_rate != model.sr:
            raise RuntimeError(f"Smoke WAV sample-rate mismatch: expected {model.sr}, decoded {decoded_rate}")
        _readiness["smoke_audio"] = _validate_audio(decoded, decoded_rate)
        _readiness["synthesis_ready"] = True
    except Exception as exc:
        requested = DEVICE.casefold().startswith("cuda")
        prefix = "CUDA synthesis failed" if requested and torch.cuda.is_available() else "Synthesis readiness failed"
        _readiness["failure"] = f"{prefix}: {exc}"


@app.on_event("startup")
def startup_smoke_test() -> None:
    run_startup_smoke_test()


@app.get("/health")
def health() -> dict[str, Any]:
    cuda_visible = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_visible else None
    capability = torch.cuda.get_device_capability(0) if cuda_visible else None
    parameters = _readiness["model_parameters"]
    resolved_devices = sorted({item["device"] for item in parameters})
    return {
        "ok": bool(_readiness["synthesis_ready"]),
        "provider": "chatterbox",
        "torch_version": torch.__version__,
        "torch_cuda_version": torch.version.cuda,
        "torchaudio_version": torchaudio.__version__,
        "cudnn_version": torch.backends.cudnn.version(),
        "cuda_visible": cuda_visible,
        "gpu_name": gpu_name,
        "gpu_compute_capability": ".".join(map(str, capability)) if capability else None,
        "requested_device": DEVICE,
        "resolved_device": ", ".join(resolved_devices) if resolved_devices else None,
        "model_loaded": bool(_readiness["model_loaded"]),
        "synthesis_ready": bool(_readiness["synthesis_ready"]),
        "model_parameters": parameters,
        "smoke_audio": _readiness["smoke_audio"],
        "failure": _readiness["failure"],
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
