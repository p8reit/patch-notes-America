import asyncio
import io
import json
import struct
import wave
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main


HOSTS = [{"id": "a" * 32, "name": "Alex", "voice": "default", "tempo": 1.0}]


def _episode() -> dict:
    return {
        "title": "Deterministic episode",
        "hosts": HOSTS,
        "research_packet": {
            "episode_angle": "Why this matters",
            "stories": [{"headline": "The lead", "verified_facts": "A checked fact."}],
        },
        "story_notes": "Source notes",
        "target_minutes": 8,
        "conversation_tone": "measured",
        "script": "[Alex]\nWelcome to the show.",
        "image_prompt": "A newsroom",
        "intro_lines": "This is Patch Notes.",
        "intro_overlap": False,
        "intro_music_volume": 0.25,
    }


def _packet() -> dict:
    return {
        "title": "Primary sources",
        "episode_angle": "Follow the evidence",
        "stories": [{"headline": "The lead", "verified_facts": "A checked fact."}],
    }


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<h", 8000 if index % 2 else -8000) for index in range(800)))
    return output.getvalue()


def test_complete_episode_and_research_packet_route_round_trips(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    packets = tmp_path / "packets"
    saved.mkdir()
    packets.mkdir()
    monkeypatch.setattr(main, "SAVED_EPISODES_DIR", saved)
    monkeypatch.setattr(main, "RESEARCH_PACKETS_DIR", packets)
    client = TestClient(main.app)

    episode_response = client.post(
        "/api/saved-episodes",
        data={"episode_json": json.dumps(_episode())},
        files={"intro_track": ("theme.mp3", b"intro", "audio/mpeg")},
    )
    assert episode_response.status_code == 200
    episode_id = episode_response.json()["id"]
    assert client.get("/api/saved-episodes").json()["episodes"][0]["id"] == episode_id
    restored = client.get(f"/api/saved-episodes/{episode_id}").json()["episode"]
    assert {key: restored[key] for key in _episode() if key != "hosts"} == {
        key: value for key, value in _episode().items() if key != "hosts"
    }
    assert {key: restored["hosts"][0][key] for key in HOSTS[0]} == HOSTS[0]
    assert restored["intro_track"]["filename"] == "theme.mp3"

    packet_response = client.post(
        "/api/research-packets", data={"packet_json": json.dumps(_packet())}
    )
    assert packet_response.status_code == 200
    saved_packet = packet_response.json()
    packet_id = saved_packet["id"]
    assert client.get("/api/research-packets").json() == {
        "packets": [{"id": packet_id, "title": "Primary sources", "stories": 1}]
    }
    loaded_packet = client.get(f"/api/research-packets/{packet_id}").json()
    assert loaded_packet["packet"] == saved_packet["packet"]
    assert "A checked fact." in loaded_packet["notes"]


def test_generation_job_create_list_and_read_without_external_services(tmp_path, monkeypatch):
    queued = []

    async def ready():
        return None

    async def enqueue(job_id):
        queued.append(job_id)

    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "require_chatterbox_ready", ready)
    monkeypatch.setattr(main, "queue_generation_job", enqueue)
    client = TestClient(main.app)

    response = client.post(
        "/api/generation-jobs",
        data={
            "title": "Mock render",
            "script": "[Alex]\nA deterministic line.",
            "hosts_json": json.dumps(HOSTS),
        },
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert queued == [job_id]
    assert (tmp_path / job_id / "script.txt").read_text() == "[Alex]\nA deterministic line."
    listed = client.get("/api/generation-jobs").json()["jobs"]
    assert [(item["id"], item["status"]) for item in listed] == [(job_id, "queued")]
    detail = client.get(f"/api/generation-jobs/{job_id}").json()
    assert detail["progress"] == {"complete": 0, "total": 1}
    assert detail["chunks"][0]["text"] == "A deterministic line."


def test_generation_job_persists_resolved_single_host_performance(tmp_path, monkeypatch):
    async def ready():
        return None

    async def enqueue(_job_id):
        return None

    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "require_chatterbox_ready", ready)
    monkeypatch.setattr(main, "queue_generation_job", enqueue)
    performance = {
        "enabled": True,
        "beats": [{"id": "beat-one", "text": "This is incredible!", "preset": "excited", "intensity": 0.8}],
    }

    response = TestClient(main.app).post("/api/generation-jobs", data={
        "title": "Expressive render", "script": "[Alex]\nThis is incredible!",
        "hosts_json": json.dumps(HOSTS), "performance_json": json.dumps(performance),
    })

    assert response.status_code == 202
    job = json.loads((tmp_path / response.json()["job_id"] / "job.json").read_text())
    assert job["performance"]["enabled"] is True
    assert job["chunks"][0]["performance"]["beat_id"] == "beat-one"
    assert job["chunks"][0]["exaggeration"] == 0.7
    assert job["chunks"][0]["cfg_weight"] == 0.436


def test_restart_requeues_interrupted_work_and_preserves_valid_completed_chunk(tmp_path, monkeypatch):
    job_dir = tmp_path / "recover-me"
    chunk_dir = job_dir / "chunks"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "kept.wav").write_bytes(_wav_bytes())
    job = {
        "id": "recover-me",
        "status": "running",
        "error": "container stopped",
        "started_at": "before-restart",
        "chunks": [
            {"status": "complete", "output": "chunks/kept.wav", "normalized_output": "old"},
            {"status": "running", "output": "chunks/missing.wav", "error": "interrupted"},
        ],
    }
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "JOB_WORKERS", 0)

    asyncio.run(main.start_generation_workers())
    try:
        recovered = json.loads((job_dir / "job.json").read_text())
        assert recovered["status"] == "queued"
        assert recovered["chunks"][0]["status"] == "complete"
        assert recovered["chunks"][0]["output"] == "chunks/kept.wav"
        assert recovered["chunks"][1]["status"] == "queued"
        assert recovered["chunks"][1]["output"] is None
        assert main.generation_queue.get_nowait() == "recover-me"
    finally:
        asyncio.run(main.stop_generation_workers())


def test_completed_chunk_is_reused_during_processing(tmp_path, monkeypatch):
    job_dir = tmp_path / "reuse"
    normalized = job_dir / "chunks" / "normalized" / "utterance-chunk-001.wav"
    normalized.parent.mkdir(parents=True)
    raw = job_dir / "chunks" / "utterance-chunk-001.wav"
    raw.write_bytes(_wav_bytes())
    normalized.write_bytes(_wav_bytes())
    job = {
        "id": "reuse", "title": "Reuse", "status": "queued", "intro": {},
        "chunks": [{
            "id": "chunk-001", "number": 1, "status": "complete",
            "output": "chunks/utterance-chunk-001.wav",
            "normalized_output": "chunks/normalized/utterance-chunk-001.wav",
            "audio_metrics": {"raw_duration": 1.0}, "host": "Alex", "text": "Already done",
            "section": "episode", "transition": {}, "dramatic_pause_after": False,
        }],
    }
    main._write_job(job_dir, job)
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("completed chunk must not call Chatterbox")

    def concatenate(_paths, _chunks, destination):
        destination.write_bytes(_wav_bytes())
        return [{"index": 1, "speaker": "Alex", "text_length": 12, "duration": 1.0,
                 "start": 0.0, "end": 1.0, "pause_after_ms": 0}]

    def assemble(_paths, destination, _chunks):
        destination.write_bytes(b"ID3mock")

    async def clips(*_args, **_kwargs):
        return []

    monkeypatch.setattr(main, "synthesize_chunk_with_retry", forbidden)
    monkeypatch.setattr(main, "concatenate_wav_segments", concatenate)
    monkeypatch.setattr(main, "analyze_final_audio_silence", lambda _path: [])
    monkeypatch.setattr(main, "assemble_mp3", assemble)
    monkeypatch.setattr(main, "render_automatic_social_clips", clips)

    asyncio.run(main.process_generation_job("reuse"))

    finished = json.loads((job_dir / "job.json").read_text())
    assert finished["status"] == "complete", finished.get("error")
    assert finished["chunks"][0]["status"] == "complete"
    assert (job_dir / "reuse.mp3").read_bytes() == b"ID3mock"


def test_episode_and_clip_downloads_and_missing_assets(tmp_path, monkeypatch):
    episode = tmp_path / "done"
    clips = episode / "clips"
    clips.mkdir(parents=True)
    (episode / "done.mp3").write_bytes(b"ID3episode")
    (clips / "highlight.mp4").write_bytes(b"video")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    client = TestClient(main.app)

    mp3 = client.get("/api/episodes/done/download")
    assert (mp3.status_code, mp3.headers["content-type"], mp3.content) == (
        200, "audio/mpeg", b"ID3episode"
    )
    clip = client.get("/api/episodes/done/clips/highlight/download")
    assert (clip.status_code, clip.headers["content-type"], clip.content) == (
        200, "video/mp4", b"video"
    )
    assert client.get("/api/episodes/unknown/download").status_code == 404
    assert client.get("/api/episodes/done/clips/missing/download").status_code == 404
    assert client.get("/api/episodes/done/clips/bad%2Fid/download").status_code in {400, 404}


def test_invalid_ids_malformed_manifests_and_provider_failure_are_bounded(tmp_path, monkeypatch):
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "job.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    client = TestClient(main.app)

    assert client.get("/api/generation-jobs").json() == {"jobs": [], "workers": main.JOB_WORKERS}
    assert client.get("/api/generation-jobs/bad%2Fid").status_code in {400, 404}
    assert client.get("/api/generation-jobs/missing").status_code == 404

    async def unavailable():
        raise HTTPException(status_code=502, detail="Chatterbox provider is offline")

    monkeypatch.setattr(main, "require_chatterbox_ready", unavailable)
    response = client.post(
        "/api/generation-jobs",
        data={"title": "Unavailable", "script": "[Alex]\nHello", "hosts_json": json.dumps(HOSTS)},
    )
    assert response.status_code == 502
    assert not [path for path in tmp_path.iterdir() if path.name != "malformed"]
