import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main
from fastapi import HTTPException


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 2400)
    return output.getvalue()


def configure_repository(monkeypatch, tmp_path: Path, count: int = 1):
    config = tmp_path / "config"
    voices = tmp_path / "data" / "voices"
    output = tmp_path / "output"
    config.mkdir(); voices.mkdir(parents=True); output.mkdir()
    monkeypatch.setattr(main, "HOST_PROFILES_FILE", config / "host_profiles.json")
    monkeypatch.setattr(main, "VOICE_DIR", voices)
    monkeypatch.setattr(main, "OUTPUT_DIR", output)
    hosts = main.parse_hosts(json.dumps([
        {"id": f"{number:032x}", "name": f"Host {number}", "voice": "default", "tempo": 1.0}
        for number in range(1, count + 1)
    ]))
    main.save_host_profiles(hosts)
    return hosts, voices


def test_upload_stores_normalized_reference_and_persists(monkeypatch, tmp_path):
    hosts, voices = configure_repository(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "normalize_reference_audio", lambda source, destination: destination.write_bytes(source.read_bytes()))
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/hosts/{hosts[0]['id']}/voice",
            files={"audio": ("voice.wav", wav_bytes(), "audio/wav")},
            data={"exaggeration": "0.55", "cfg_weight": "0.35"},
        )
    assert response.status_code == 200, response.text
    canonical = voices / hosts[0]["id"] / "reference.wav"
    assert canonical.read_bytes().startswith(b"RIFF")
    reloaded = main.load_host_profiles()[0]
    assert reloaded["reference_audio_path"] == f"voices/{hosts[0]['id']}/reference.wav"
    assert reloaded["exaggeration"] == 0.55
    assert response.json()["reference_voice"]["filename"] == "voice.wav"


def test_upload_rejects_unsupported_and_corrupt_audio(monkeypatch, tmp_path):
    hosts, _ = configure_repository(monkeypatch, tmp_path)
    def reject_audio(source, destination):
        raise HTTPException(status_code=400, detail="The selected file could not be decoded as audio.")
    monkeypatch.setattr(main, "normalize_reference_audio", reject_audio)
    with TestClient(main.app) as client:
        unsupported = client.post(
            f"/api/hosts/{hosts[0]['id']}/voice",
            files={"audio": ("malware.exe", b"MZ", "application/octet-stream")},
        )
        corrupt = client.post(
            f"/api/hosts/{hosts[0]['id']}/voice",
            files={"audio": ("broken.wav", b"not audio", "audio/wav")},
        )
    assert unsupported.status_code == 415
    assert "WAV, MP3, FLAC, or M4A" in unsupported.json()["detail"]
    assert corrupt.status_code == 400
    assert "could not be decoded" in corrupt.json()["detail"]


def test_mp3_normalization_targets_pcm_24khz_mono(monkeypatch, tmp_path):
    source = tmp_path / "voice.mp3"
    destination = tmp_path / "host" / "reference.wav"
    source.write_bytes(b"fake mp3 passed to mocked ffmpeg")
    command = []

    def fake_run(args, **kwargs):
        command.extend(args)
        Path(args[-1]).write_bytes(wav_bytes())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    main.normalize_reference_audio(source, destination)
    assert destination.read_bytes().startswith(b"RIFF")
    assert command[command.index("-ar") + 1] == "24000"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"


def test_remove_returns_host_to_default_voice(monkeypatch, tmp_path):
    hosts, voices = configure_repository(monkeypatch, tmp_path)
    host = main.load_host_profiles()[0]
    path = voices / host["id"] / "reference.wav"
    path.parent.mkdir(); path.write_bytes(wav_bytes())
    host["reference_audio_path"] = f"voices/{host['id']}/reference.wav"
    main.save_host_profiles([host])
    with TestClient(main.app) as client:
        response = client.delete(f"/api/hosts/{host['id']}/voice")
    assert response.status_code == 200
    assert not path.exists()
    assert main.load_host_profiles()[0]["reference_audio_path"] is None
    assert main.build_speech_chunks("Hello", main.load_host_profiles())[0]["voice"] == "default"


def test_each_speaker_resolves_to_its_host_reference(monkeypatch, tmp_path):
    hosts, voices = configure_repository(monkeypatch, tmp_path, count=2)
    for host in hosts:
        path = voices / host["id"] / "reference.wav"
        path.parent.mkdir(); path.write_bytes(wav_bytes())
        host["reference_audio_path"] = f"voices/{host['id']}/reference.wav"
    chunks = main.build_speech_chunks("First.\n\n[Host 2]\nSecond.", hosts)
    assert [chunk["host_id"] for chunk in chunks] == [hosts[0]["id"], hosts[1]["id"]]
    assert [chunk["voice"] for chunk in chunks] == [f"host-{hosts[0]['id']}", f"host-{hosts[1]['id']}"]


def test_missing_reference_logs_and_falls_back(monkeypatch, tmp_path, caplog):
    hosts, _ = configure_repository(monkeypatch, tmp_path)
    hosts[0]["reference_audio_path"] = f"voices/{hosts[0]['id']}/reference.wav"
    with caplog.at_level("WARNING"):
        chunk = main.build_speech_chunks("Still render this.", hosts)[0]
    assert chunk["voice"] == "default"
    assert "Falling back to default voice" in caplog.text


def test_preview_uses_current_ui_settings_and_host_reference(monkeypatch, tmp_path):
    hosts, voices = configure_repository(monkeypatch, tmp_path)
    host = hosts[0]
    path = voices / host["id"] / "reference.wav"
    path.parent.mkdir(); path.write_bytes(wav_bytes())
    host["reference_audio_path"] = f"voices/{host['id']}/reference.wav"
    main.save_host_profiles([host])
    received = {}

    async def fake_synthesize(text, voice, tempo, destination, **settings):
        received.update(text=text, voice=voice, tempo=tempo, **settings)
        destination.write_bytes(wav_bytes())

    monkeypatch.setattr(main, "synthesize_chunk", fake_synthesize)
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/hosts/{host['id']}/voice/preview",
            data={"exaggeration": "0.65", "cfg_weight": "0.30"},
        )
    assert response.status_code == 200
    assert received["voice"] == f"host-{host['id']}"
    assert received["exaggeration"] == 0.65
    assert received["cfg_weight"] == 0.30


def test_host_editor_exposes_end_to_end_voice_controls():
    template = (Path(__file__).parents[1] / "app/templates/index.html").read_text()
    assert 'class="voice-file"' in template
    assert "Play Reference" in template
    assert "Preview Voice" in template
    assert "remove-reference" in template
    assert "Uploading reference voice..." in template
