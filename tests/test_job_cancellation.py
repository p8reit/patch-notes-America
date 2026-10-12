import asyncio
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main


def make_job(root, job_id="cancel-me", status="queued"):
    job_dir = root / job_id
    chunks = job_dir / "chunks"
    chunks.mkdir(parents=True)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x10" * 800)
    (chunks / "kept.wav").write_bytes(output.getvalue())
    (chunks / "normalized").mkdir()
    (chunks / "normalized" / "kept.wav").write_bytes(output.getvalue())
    job = {
        "id": job_id, "title": "Cancel me", "status": status, "intro": {}, "image_prompt": "",
        "chunks": [{
            "id": "chunk-001", "status": "complete", "output": "chunks/kept.wav",
            "normalized_output": "chunks/normalized/kept.wav", "audio_metrics": {"raw_duration": 0.1},
            "host": "Alex", "text": "Retained speech", "section": "episode", "transition": {},
        }],
    }
    main._write_job(job_dir, job)
    return job_dir, job


def test_queued_cancellation_is_durable_idempotent_and_skipped_on_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "JOB_WORKERS", 0)
    job_dir, job = make_job(tmp_path)
    job["chunks"].append({"status": "queued", "output": None})
    main._write_job(job_dir, job)
    client = TestClient(main.app)
    response = client.post("/api/generation-jobs/cancel-me/cancel")
    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["progress"] == {"complete": 1, "total": 2}
    assert cancelled["chunks"][1]["status"] == "cancelled"
    assert client.post("/api/generation-jobs/cancel-me/cancel").json() == cancelled
    assert client.get("/api/generation-jobs/cancel-me").json() == cancelled
    assert client.get("/api/generation-jobs").json()["jobs"][0]["status"] == "cancelled"
    assert (job_dir / "chunks/kept.wav").exists()

    async def restart():
        await main.start_generation_workers()
        try:
            assert main.generation_queue.empty()
            # A stale queue entry cannot revive a cancelled job either.
            await main.process_generation_job("cancel-me")
        finally:
            await main.stop_generation_workers()

    asyncio.run(restart())
    assert json.loads((job_dir / "job.json").read_text())["status"] == "cancelled"


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_finished_jobs_cannot_be_cancelled(tmp_path, monkeypatch, status):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    job_dir, _job = make_job(tmp_path, status=status)
    original = (job_dir / "job.json").read_bytes()
    assert TestClient(main.app).post("/api/generation-jobs/cancel-me/cancel").status_code == 409
    assert (job_dir / "job.json").read_bytes() == original


def test_cancel_validates_job_id_and_missing_job(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    client = TestClient(main.app)
    assert client.post("/api/generation-jobs/bad%20id/cancel").status_code == 400
    assert client.post("/api/generation-jobs/missing/cancel").status_code == 404


@pytest.mark.parametrize("stage", ["speech", "retry", "artwork"])
def test_running_cancellation_unblocks_worker_and_keeps_completed_audio(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "JOB_WORKERS", 1)
    job_dir, job = make_job(tmp_path)
    if stage in {"speech", "retry"}:
        job["chunks"].append({
            "id": "chunk-002", "status": "queued", "text": "Pending", "voice": "default", "tempo": 1,
        })
        main._write_job(job_dir, job)

    entered = asyncio.Event()
    stopped = asyncio.Event()
    next_finished = asyncio.Event()
    original_process = main.process_generation_job

    async def process(job_id):
        if job_id == "next-job":
            next_finished.set()
        else:
            await original_process(job_id)

    async def blocked_speech(_text, _voice, _tempo, destination, **_settings):
        destination.write_bytes(b"partial")
        entered.set()
        try:
            if stage == "retry":
                raise httpx.ReadTimeout("Transient failure")
            await asyncio.Event().wait()
        finally:
            stopped.set()

    class BlockedImages:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            stopped.set()

        def __init__(self, **_kwargs):
            pass

        async def post(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

    def concatenate(_paths, _chunks, destination):
        destination.write_bytes(b"mock timeline")
        return [{"index": 1, "speaker": "Alex", "text_length": 15, "duration": 1,
                 "start": 0, "end": 1, "pause_after_ms": 0}]

    monkeypatch.setattr(main, "process_generation_job", process)
    monkeypatch.setattr(main, "synthesize_chunk", blocked_speech)
    monkeypatch.setattr(main, "TTS_RETRY_DELAY_SECONDS", 60)
    monkeypatch.setattr(main, "validate_audio_segment", lambda _path: (True, ""))
    monkeypatch.setattr(main, "concatenate_wav_segments", concatenate)
    monkeypatch.setattr(main, "analyze_final_audio_silence", lambda _path: [])
    monkeypatch.setattr(main, "assemble_mp3", lambda _paths, path, _chunks: path.write_bytes(b"ID3retained"))
    monkeypatch.setattr(main, "CLIP_IMAGE_PROVIDER", "local")
    monkeypatch.setattr(main, "select_automatic_clip_windows", lambda _metadata: [{"start": 0, "end": 10, "kind": "opening"}])
    monkeypatch.setattr(main, "_clip_visual_cards", lambda *_args: [{"text": "An excerpt"}])
    monkeypatch.setattr(main.httpx, "AsyncClient", BlockedImages)
    monkeypatch.setattr(main, "render_social_clip", lambda *_args: pytest.fail("Cancelled art must not render a clip"))

    async def run():
        await main.start_generation_workers()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await main.queue_generation_job("next-job")
            cancelled = await main.cancel_generation_job("cancel-me")
            assert cancelled["status"] == "cancelled"
            assert stopped.is_set()
            await asyncio.wait_for(next_finished.wait(), 2)
            await asyncio.wait_for(main.generation_queue.join(), 2)
            assert not main.generation_workers[0].done()
            assert not main.generation_tasks
        finally:
            await main.stop_generation_workers()

    asyncio.run(run())
    saved = json.loads((job_dir / "job.json").read_text())
    assert saved["status"] == "cancelled"
    assert saved["chunks"][0]["status"] == "complete"
    assert (job_dir / "chunks/kept.wav").exists()
    assert not list(job_dir.rglob("*.tmp"))
    if stage in {"speech", "retry"}:
        assert saved["chunks"][1]["status"] == "cancelled"
        assert saved["chunks"][1]["attempt"] == 1
    if stage == "artwork":
        assert saved["download_url"] == "/api/episodes/cancel-me/download"
        assert TestClient(main.app).get(saved["download_url"]).content == b"ID3retained"


def test_worker_shutdown_keeps_running_job_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "JOB_WORKERS", 1)
    job_dir, job = make_job(tmp_path)
    job["chunks"] = [{"id": "chunk-001", "status": "queued", "text": "Pending", "voice": "default", "tempo": 1}]
    main._write_job(job_dir, job)
    entered = asyncio.Event()

    async def speech(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "synthesize_chunk", speech)

    async def run():
        await main.start_generation_workers()
        await asyncio.wait_for(entered.wait(), 2)
        await main.stop_generation_workers()

    asyncio.run(run())
    saved = json.loads((job_dir / "job.json").read_text())
    assert saved["status"] == "running"
    assert saved["chunks"][0]["status"] == "running"
    assert not main.generation_tasks
