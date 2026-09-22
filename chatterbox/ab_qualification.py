"""Qualify Chatterbox CPU/CUDA and sampler paths with identical input.

Run inside the Chatterbox image with::

    python /app/ab_qualification.py --text "The qualification sentence."

The process exits non-zero if either CUDA itself or the private sampler adapter
produces invalid audio. It intentionally uses the server's exact contracts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import torch
from chatterbox.tts import ChatterboxTTS

import server

DIAGNOSTIC_TEXT = "The quick brown fox checks this short diagnostic render for clear, natural speech."
DIAGNOSTIC_SEED = 2026
DIAGNOSTIC_REFERENCE_VOICE = os.getenv("CHATTERBOX_DIAGNOSTIC_VOICE", "").strip() or None


def diagnose(results: dict[str, dict[str, Any]]) -> str:
    """Attribute a failed A/B result to CUDA, the adapter, or both."""
    if not results["cpu_baseline"]["ok"]:
        return "inconclusive: CPU baseline failed"
    if not results["cuda_baseline"]["ok"]:
        return "cuda execution"
    adapter_failures = [
        name for name in ("cpu_advanced", "cuda_advanced") if not results[name]["ok"]
    ]
    if adapter_failures:
        return "internal-method override"
    return "none"


def _synthesize(device: str, mode: str, text: str, seed: int, prompt: str | None) -> tuple[dict[str, Any], Any | None, int | None]:
    try:
        torch.manual_seed(seed)
        if device == "cuda":
            torch.cuda.manual_seed_all(seed)
        model = ChatterboxTTS.from_pretrained(device=device)
        contract = server._build_generation_contract(model, mode)
        request = server.SpeechRequest(input=text, seed=seed)
        with torch.inference_mode():
            wav = server.generate_audio(model, request, prompt, mode=mode, contract=contract)
        return {"ok": True, **server._validate_audio(wav, model.sr)}, wav, model.sr
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, locals().get("wav"), getattr(locals().get("model"), "sr", None)


def run(text: str = DIAGNOSTIC_TEXT, seed: int = DIAGNOSTIC_SEED, prompt: str | None = DIAGNOSTIC_REFERENCE_VOICE, artifact_dir: Path | None = None) -> dict[str, Any]:
    rendered = {
        f"{device}_{mode}": _synthesize(device, mode, text, seed, prompt)
        for mode in ("baseline", "advanced")
        for device in ("cpu", "cuda")
    }
    results = {name: item[0] for name, item in rendered.items()}
    report = {
        "text": text, "seed": seed, "reference_voice": prompt,
        "settings": server.SpeechRequest(input=text, seed=seed).model_dump(exclude={"input", "seed"}),
        "results": results, "diagnosis": diagnose(results),
    }
    if report["diagnosis"] != "none" and artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name, (_, wav, sample_rate) in rendered.items():
            if wav is not None and sample_rate:
                # Keep even the render that failed validation; it is the most
                # useful evidence for comparing CPU and CUDA outside the app.
                torch_audio = getattr(server, "torchaudio")
                torch_audio.save(str(artifact_dir / f"{name}.wav"), wav.detach().cpu(), sample_rate)
        (artifact_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default=DIAGNOSTIC_TEXT)
    parser.add_argument("--seed", type=int, default=DIAGNOSTIC_SEED)
    parser.add_argument("--reference-voice", default=DIAGNOSTIC_REFERENCE_VOICE)
    parser.add_argument("--artifact-dir", type=Path, default=Path("/tmp/chatterbox-qualification-failure"))
    args = parser.parse_args()
    report = run(args.text, args.seed, args.reference_voice, args.artifact_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["diagnosis"] == "none" else 1


if __name__ == "__main__":
    raise SystemExit(main())
