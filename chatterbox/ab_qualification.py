"""Qualify Chatterbox CPU/CUDA and sampler paths with identical input.

Run inside the Chatterbox image with::

    python /app/ab_qualification.py --text "The qualification sentence."

The process exits non-zero if either CUDA itself or the private sampler adapter
produces invalid audio. It intentionally uses the server's exact contracts.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import torch
from chatterbox.tts import ChatterboxTTS

import server


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


def _synthesize(device: str, mode: str, text: str, seed: int) -> dict[str, Any]:
    try:
        torch.manual_seed(seed)
        if device == "cuda":
            torch.cuda.manual_seed_all(seed)
        model = ChatterboxTTS.from_pretrained(device=device)
        contract = server._build_generation_contract(model, mode)
        request = server.SpeechRequest(input=text)
        with torch.inference_mode():
            wav = server.generate_audio(model, request, None, mode=mode, contract=contract)
        return {"ok": True, **server._validate_audio(wav, model.sr)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run(text: str, seed: int) -> dict[str, Any]:
    results = {
        f"{device}_{mode}": _synthesize(device, mode, text, seed)
        for mode in ("baseline", "advanced")
        for device in ("cpu", "cuda")
    }
    return {"text": text, "seed": seed, "results": results, "diagnosis": diagnose(results)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="Identical CPU and CUDA qualification text.")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    report = run(args.text, args.seed)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["diagnosis"] == "none" else 1


if __name__ == "__main__":
    raise SystemExit(main())
