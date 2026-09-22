#!/usr/bin/env python3
"""Produce the repeatable CPU/CUDA Chatterbox qualification matrix."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch
import torchaudio
from chatterbox.tts import ChatterboxTTS

import server


SEED = 20260922
SHORT_TEXT = "A steady signal makes faults easier to find."
MEDIUM_TEXT = (
    "The qualification run uses identical words and sampling controls on every path, "
    "so a change in the recording can be traced to the device or integration layer. "
    "It also keeps the reference voice and output measurements with the evidence, "
    "making the result straightforward to compare before GPU inference becomes the default."
)
SETTINGS = {
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "min_p": 0.05,
    "top_p": 1.0,
    "repetition_penalty": 1.2,
}


def slug(value: str) -> str:
    """Return the deliberately small filename vocabulary used by artifacts."""
    return value.replace("_", "-")


def normalize_reference(source: Path, destination: Path) -> dict[str, Any]:
    """Create a stable 24 kHz mono, peak-normalized reference WAV."""
    audio, source_rate = torchaudio.load(str(source))
    audio = audio.detach().float().cpu().mean(dim=0, keepdim=True)
    if source_rate != 24_000:
        audio = torchaudio.functional.resample(audio, source_rate, 24_000)
    if not audio.numel() or not torch.isfinite(audio).all():
        raise ValueError("reference voice is empty or contains non-finite samples")
    peak = float(audio.abs().max())
    if peak <= 0:
        raise ValueError("reference voice is silent")
    audio = audio * (0.95 / peak)
    torchaudio.save(str(destination), audio, 24_000, encoding="PCM_S", bits_per_sample=16)
    return audio_metrics(audio, 24_000, destination)


def audio_metrics(audio: torch.Tensor, sample_rate: int, path: Path) -> dict[str, Any]:
    """Return objective measurements and a content digest for one artifact."""
    samples = audio.detach().float().cpu().reshape(-1)
    finite = torch.isfinite(samples)
    safe = samples[finite]
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": sample_rate,
        "channels": int(audio.shape[0]) if audio.ndim > 1 else 1,
        "samples": int(samples.numel()),
        "duration_seconds": round(samples.numel() / sample_rate, 6),
        "non_finite_samples": int((~finite).sum()),
        "peak": round(float(safe.abs().max()), 8) if safe.numel() else None,
        "rms": round(float(torch.sqrt(torch.mean(safe.square()))), 8) if safe.numel() else None,
        "dc_offset": round(float(safe.mean()), 8) if safe.numel() else None,
        "clipping_ratio": round(float((safe.abs() >= 0.999).float().mean()), 8) if safe.numel() else None,
    }


def environment_metadata() -> dict[str, Any]:
    """Capture the software and visible GPU stack without making it a prerequisite."""
    try:
        nvidia_smi = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        gpu_inventory = nvidia_smi.stdout.strip() or nvidia_smi.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        gpu_inventory = f"unavailable: {type(exc).__name__}: {exc}"
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "chatterbox_tts": importlib.metadata.version("chatterbox-tts"),
        "torch": torch.__version__,
        "torchaudio": torchaudio.__version__,
        "torch_cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "cuda_available": torch.cuda.is_available(),
        "gpu_inventory": gpu_inventory,
        "argv": sys.argv,
    }


def seed_everything(device: str) -> None:
    torch.manual_seed(SEED)
    if device == "cuda" and torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)
    if hasattr(torch, "use_deterministic_algorithms"):
        torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def render_case(
    model: Any, device: str, path_name: str, voice_name: str, case_name: str,
    text: str, prompt: str | None, output: Path,
) -> dict[str, Any]:
    filename = f"{slug(device)}_{slug(path_name)}_{slug(voice_name)}_{slug(case_name)}.wav"
    destination = output / filename
    started = time.monotonic()
    try:
        seed_everything(device)
        request = server.SpeechRequest(input=text, seed=SEED, **SETTINGS)
        with torch.inference_mode():
            if path_name == "direct":
                wav = model.generate(
                    text, audio_prompt_path=prompt,
                    exaggeration=SETTINGS["exaggeration"],
                    cfg_weight=SETTINGS["cfg_weight"],
                    temperature=SETTINGS["temperature"],
                )
            else:
                contract = server._build_generation_contract(model, "advanced")
                wav = server.generate_audio(
                    model, request, prompt, mode="advanced", contract=contract,
                )
        wav = wav.detach().cpu()
        torchaudio.save(str(destination), wav, model.sr, encoding="PCM_S", bits_per_sample=16)
        decoded, rate = torchaudio.load(str(destination))
        metrics = audio_metrics(decoded, rate, destination)
        server._validate_audio(decoded, rate)
        return {"ok": True, "elapsed_seconds": round(time.monotonic() - started, 3), "metrics": metrics}
    except Exception as exc:
        return {
            "ok": False, "file": filename,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def diagnosis(results: dict[str, dict[str, Any]]) -> dict[str, str]:
    direct_cuda = [value for key, value in results.items() if key.startswith("cuda/direct/")]
    adapter_cuda = [value for key, value in results.items() if key.startswith("cuda/adapter/")]
    if not direct_cuda or not all(item["ok"] for item in direct_cuda):
        return {"focus": "CUDA/Torch/GPU compatibility stack", "reason": "direct CUDA failed"}
    if not adapter_cuda or not all(item["ok"] for item in adapter_cuda):
        return {
            "focus": "generate_audio, sampler injection, tensor conversion, and encoding",
            "reason": "direct CUDA succeeded but adapter CUDA failed",
        }
    return {
        "focus": "synthesize_chunk, normalization, and final assembly if episode output still fails",
        "reason": "both CUDA paths succeeded",
    }


def run(reference: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    normalized = output / "reference_normalized.wav"
    reference_metrics = normalize_reference(reference, normalized)
    results: dict[str, dict[str, Any]] = {}
    cases = {"short-sentence": SHORT_TEXT, "medium-paragraph": MEDIUM_TEXT}
    voices = {"builtin": None, "reference": str(normalized)}

    for device in ("cpu", "cuda"):
        if device == "cuda" and not torch.cuda.is_available():
            error = "RuntimeError: CUDA is not available to the pinned PyTorch build"
            for path_name in ("direct", "adapter"):
                for voice_name in voices:
                    for case_name in cases:
                        results[f"{device}/{path_name}/{voice_name}/{case_name}"] = {"ok": False, "error": error}
            continue
        try:
            seed_everything(device)
            model = ChatterboxTTS.from_pretrained(device=device)
            for path_name in ("direct", "adapter"):
                for voice_name, prompt in voices.items():
                    for case_name, text in cases.items():
                        key = f"{device}/{path_name}/{voice_name}/{case_name}"
                        results[key] = render_case(
                            model, device, path_name, voice_name, case_name, text, prompt, output,
                        )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            for path_name in ("direct", "adapter"):
                for voice_name in voices:
                    for case_name in cases:
                        results.setdefault(f"{device}/{path_name}/{voice_name}/{case_name}", {"ok": False, "error": error})
        finally:
            if "model" in locals():
                del model
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    passed = all(item["ok"] for item in results.values())
    report = {
        "acceptance_gate": {"passed": passed, "required_cases": len(results), "passed_cases": sum(r["ok"] for r in results.values())},
        "diagnosis": diagnosis(results),
        "environment": environment_metadata(),
        "fixture": {"seed": SEED, "settings": SETTINGS, "texts": cases, "source_reference": str(reference), "normalized_reference": reference_metrics},
        "results": results,
    }
    (output / "qualification-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-voice", required=True, type=Path, help="authorized reference audio")
    parser.add_argument("--output-dir", type=Path, default=Path("/artifacts/chatterbox-qualification"))
    args = parser.parse_args()
    report = run(args.reference_voice, args.output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["acceptance_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
