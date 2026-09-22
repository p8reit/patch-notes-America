# syntax=docker/dockerfile:1
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/huggingface \
    EXPECTED_PYTHON_MINOR=3.10 \
    EXPECTED_CHATTERBOX_VERSION=0.1.6 \
    EXPECTED_TORCH_VERSION=2.6.0+cu124 \
    EXPECTED_TORCHAUDIO_VERSION=2.6.0+cu124 \
    EXPECTED_TORCH_CUDA_VERSION=12.4

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /service
COPY chatterbox/requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt \
    && python3 - <<'PY' | tee /image-build-versions.txt
import importlib.metadata
import os
import platform

import torch
import torchaudio

versions = {
    "Python": platform.python_version(),
    "Chatterbox": importlib.metadata.version("chatterbox-tts"),
    "Torch": torch.__version__,
    "TorchAudio": torchaudio.__version__,
    "CUDA": torch.version.cuda or "none",
    "cuDNN": str(torch.backends.cudnn.version() or "none"),
}
for name, version in versions.items():
    print(f"{name}={version}")

assert ".".join(platform.python_version_tuple()[:2]) == os.environ["EXPECTED_PYTHON_MINOR"], versions
assert versions["Chatterbox"] == os.environ["EXPECTED_CHATTERBOX_VERSION"], versions
assert torch.__version__ == os.environ["EXPECTED_TORCH_VERSION"], versions
assert torchaudio.__version__ == os.environ["EXPECTED_TORCHAUDIO_VERSION"], versions
assert torch.version.cuda == os.environ["EXPECTED_TORCH_CUDA_VERSION"], versions
assert torch.backends.cudnn.version() is not None, versions
print("Chatterbox CUDA dependency smoke check passed")
PY
COPY chatterbox/server.py chatterbox/ab_qualification.py .

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
