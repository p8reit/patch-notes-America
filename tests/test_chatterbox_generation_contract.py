import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def server(monkeypatch):
    torch = types.ModuleType("torch")
    torch.Tensor = type("Tensor", (), {})
    torch.__version__ = "2.6.0+cu124"
    torch.version = types.SimpleNamespace(cuda="12.4")
    torch.cuda = types.SimpleNamespace(is_available=lambda: True)
    torch.backends = types.SimpleNamespace(cudnn=types.SimpleNamespace(version=lambda: 90100))

    torchaudio = types.ModuleType("torchaudio")
    torchaudio.__version__ = "2.6.0+cu124"
    package = types.ModuleType("chatterbox")
    package.__path__ = []
    tts = types.ModuleType("chatterbox.tts")
    tts.ChatterboxTTS = type("ChatterboxTTS", (), {})
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    monkeypatch.setitem(sys.modules, "chatterbox", package)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", tts)

    path = Path(__file__).parents[1] / "chatterbox" / "server.py"
    spec = importlib.util.spec_from_file_location("contract_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _model(generate_parameters, inference_parameters=("min_p", "top_p", "repetition_penalty")):
    generate_source = "def generate(self, " + ", ".join(generate_parameters) + "): return self.result"
    namespace = {}
    exec(generate_source, namespace)
    inference_source = "def inference(self, " + ", ".join(inference_parameters) + "): return None"
    exec(inference_source, namespace)
    model_type = type("Model", (), {"generate": namespace["generate"]})
    model = model_type()
    model.result = object()
    model.t3 = types.SimpleNamespace(inference=types.MethodType(namespace["inference"], model))
    return model


@pytest.mark.parametrize(
    ("version", "generate_parameters", "mode", "uses_adapter"),
    [
        ("0.1.6", ("text", "audio_prompt_path", "exaggeration", "cfg_weight", "temperature"), "baseline", False),
        ("0.1.6", ("text", "audio_prompt_path", "exaggeration", "cfg_weight", "temperature"), "advanced", True),
        ("0.2.0", ("text", "audio_prompt_path", "min_p", "top_p", "repetition_penalty"), "advanced", False),
    ],
)
def test_supported_generation_contracts(server, monkeypatch, version, generate_parameters, mode, uses_adapter):
    model = _model(generate_parameters)
    monkeypatch.setattr(server, "_chatterbox_version", lambda: version)

    contract = server._build_generation_contract(model, mode)

    assert (contract.advanced_adapter is not None) is uses_adapter
    assert contract.public_parameters == frozenset(generate_parameters)


@pytest.mark.parametrize("missing", ["min_p", "top_p", "repetition_penalty"])
def test_016_adapter_rejects_each_sampler_signature_mismatch(server, monkeypatch, missing):
    model = _model(
        ("text", "audio_prompt_path", "temperature"),
        tuple(name for name in server.ADVANCED_CONTROLS if name != missing),
    )
    monkeypatch.setattr(server, "_chatterbox_version", lambda: "0.1.6")

    with pytest.raises(RuntimeError, match=f"missing: {missing}"):
        server._build_generation_contract(model, "advanced")


def test_unknown_private_api_version_fails_at_initialization(server, monkeypatch):
    model = _model(("text", "audio_prompt_path", "temperature"))
    monkeypatch.setattr(server, "_chatterbox_version", lambda: "9.9.9")

    with pytest.raises(RuntimeError, match="no tested adapter"):
        server._build_generation_contract(model, "advanced")


def test_baseline_calls_only_discovered_public_parameters(server, monkeypatch):
    calls = []

    class Model:
        def generate(self, text, audio_prompt_path=None, temperature=0.8):
            calls.append((text, audio_prompt_path, temperature))
            return "audio"

    model = Model()
    monkeypatch.setattr(server, "_chatterbox_version", lambda: "unknown-is-fine-for-baseline")
    contract = server._build_generation_contract(model, "baseline")
    request = server.SpeechRequest(input="same text", temperature=0.7)

    assert server.generate_audio(model, request, "voice.wav", mode="baseline", contract=contract) == "audio"
    assert calls == [("same text", "voice.wav", 0.7)]


@pytest.mark.parametrize(
    ("failures", "diagnosis"),
    [
        ({"cuda_baseline"}, "cuda execution"),
        ({"cuda_advanced"}, "internal-method override"),
        ({"cpu_advanced"}, "internal-method override"),
        (set(), "none"),
    ],
)
def test_ab_fixture_attributes_corruption(monkeypatch, failures, diagnosis):
    fake_server = types.ModuleType("server")
    fake_tts = types.ModuleType("chatterbox.tts")
    fake_tts.ChatterboxTTS = object
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    monkeypatch.setitem(sys.modules, "server", fake_server)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", fake_tts)
    path = Path(__file__).parents[1] / "chatterbox" / "ab_qualification.py"
    spec = importlib.util.spec_from_file_location("ab_qualification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    results = {
        name: {"ok": name not in failures}
        for name in ("cpu_baseline", "cuda_baseline", "cpu_advanced", "cuda_advanced")
    }

    assert module.diagnose(results) == diagnosis


def test_ab_fixture_preserves_fixed_input_and_failure_artifacts(monkeypatch, tmp_path):
    fake_server = types.ModuleType("server")
    fake_server.SpeechRequest = type(
        "Request", (), {
            "__init__": lambda self, **values: setattr(self, "values", values),
            "model_dump": lambda self, exclude: {
                "model": "chatterbox", "temperature": 0.8, "voice": "default"
            },
        },
    )
    saved = []
    fake_server.torchaudio = types.SimpleNamespace(
        save=lambda path, _wav, rate: (Path(path).write_bytes(b"RIFF-diagnostic"), saved.append((Path(path).name, rate)))
    )
    fake_tts = types.ModuleType("chatterbox.tts")
    fake_tts.ChatterboxTTS = object
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    monkeypatch.setitem(sys.modules, "server", fake_server)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", fake_tts)
    path = Path(__file__).parents[1] / "chatterbox" / "ab_qualification.py"
    spec = importlib.util.spec_from_file_location("ab_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Audio:
        def detach(self):
            return self

        def cpu(self):
            return self

    def synthesis(device, mode, text, seed, prompt):
        ok = not (device == "cuda" and mode == "baseline")
        return ({"ok": ok, "duration_seconds": 1.0}, Audio(), 24_000)

    monkeypatch.setattr(module, "_synthesize", synthesis)
    report = module.run(artifact_dir=tmp_path)

    assert report["text"] == module.DIAGNOSTIC_TEXT
    assert report["seed"] == module.DIAGNOSTIC_SEED
    assert report["diagnosis"] == "cuda execution"
    assert (tmp_path / "report.json").is_file()
    assert len(saved) == 4
