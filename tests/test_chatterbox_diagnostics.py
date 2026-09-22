import importlib.util
import sys
import types
from pathlib import Path

import pytest


class FakeCuda:
    def __init__(self, available=True):
        self.available = available

    def is_available(self):
        return self.available

    def get_device_name(self, _index):
        return "Test GPU"

    def get_device_capability(self, _index):
        return (8, 9)


@pytest.fixture
def server(monkeypatch):
    torch = types.ModuleType("torch")
    torch.Tensor = type("Tensor", (), {})
    torch.__version__ = "2.6.0+cu124"
    torch.version = types.SimpleNamespace(cuda="12.4")
    torch.cuda = FakeCuda()
    torch.backends = types.SimpleNamespace(cudnn=types.SimpleNamespace(version=lambda: 90100))
    torch.isfinite = lambda value: value

    torchaudio = types.ModuleType("torchaudio")
    torchaudio.__version__ = "2.6.0+cu124"
    torchaudio.save = lambda *args, **kwargs: None
    torchaudio.load = lambda *args, **kwargs: (object(), 24_000)

    package = types.ModuleType("chatterbox")
    package.__path__ = []
    tts = types.ModuleType("chatterbox.tts")
    tts.ChatterboxTTS = type("ChatterboxTTS", (), {})
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    monkeypatch.setitem(sys.modules, "chatterbox", package)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", tts)

    path = Path(__file__).parents[1] / "chatterbox" / "server.py"
    spec = importlib.util.spec_from_file_location("diagnostics_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requested_cuda_reports_actionable_failure_when_cuda_is_unavailable(server, monkeypatch):
    server.DEVICE = "cuda"
    server.torch.cuda = FakeCuda(available=False)
    monkeypatch.setattr(server, "get_model", lambda: pytest.fail("model should not load"))

    server.run_startup_smoke_test()

    result = server.health()
    assert result["cuda_visible"] is False
    assert result["model_loaded"] is False
    assert result["synthesis_ready"] is False
    assert "driver" in result["failure"]


def test_model_device_mismatch_is_rejected(server):
    parameters = [{"name": "model.weight", "device": "cpu", "dtype": "torch.float32"}]
    with pytest.raises(RuntimeError, match="requested cuda.*parameters on cpu"):
        server._check_model_placement(parameters, "cuda")


def test_unsupported_model_dtype_is_rejected(server):
    parameters = [{"name": "model.weight", "device": "cuda:0", "dtype": "torch.float64"}]
    with pytest.raises(RuntimeError, match="Unsupported model parameter dtype: torch.float64"):
        server._check_model_placement(parameters, "cuda")


def test_successful_gpu_smoke_reports_actual_parameter_placement(server, monkeypatch):
    parameter = types.SimpleNamespace(device="cuda:0", dtype="torch.float16")
    model = types.SimpleNamespace(
        sr=24_000,
        named_parameters=lambda: iter([("weight", parameter)]),
    )
    server.DEVICE = "cuda"
    monkeypatch.setattr(server, "get_model", lambda: model)
    monkeypatch.setattr(server, "generate_audio", lambda *_args: object())
    monkeypatch.setattr(server, "_validate_audio", lambda *_args: {"samples": 24_000, "duration_seconds": 1.0, "peak": 0.5})
    monkeypatch.setattr(server, "encode_wav", lambda *_args: b"RIFF-test")

    server.run_startup_smoke_test()

    result = server.health()
    assert result["ok"] is True
    assert result["cuda_visible"] is True
    assert result["model_loaded"] is True
    assert result["synthesis_ready"] is True
    assert result["resolved_device"] == "cuda:0"
    assert result["model_parameters"] == [
        {"name": "model.weight", "device": "cuda:0", "dtype": "torch.float16"}
    ]
