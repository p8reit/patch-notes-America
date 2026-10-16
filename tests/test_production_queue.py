import asyncio
import json
import subprocess
from contextlib import asynccontextmanager

import httpx
import pytest

from app import gpu, main, production_queue as production, video


def test_production_queue_browser_feedback():
    result = subprocess.run(["node", "--test", "tests/browser/production_queue.test.js"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reservations_follow_submission_order_across_features_and_nested_models(monkeypatch):
    monkeypatch.setattr(gpu, "enabled", False)

    async def exercise():
        audio, images, movies = (production.JobQueue(kind) for kind in ("audio", "image", "video"))
        audio.put_nowait("a")
        images.put_nowait("i")
        movies.put_nowait("v")
        audio.put_nowait("b")
        events = []
        active = 0

        async def process(identifier):
            nonlocal active
            active += 1
            assert active == 1
            events.append(identifier)
            # A build can switch models several times within one admitted job.
            for kind in ("speech", "image", "encoder"):
                async with gpu.lease(kind):
                    await asyncio.sleep(0)
            active -= 1

        # Deliberately start consumers in a different order from submissions.
        calls = [asyncio.create_task(queue.run(identifier, process)) for queue, identifier in
                 [(movies, "v"), (audio, "b"), (images, "i"), (audio, "a")]]
        await asyncio.wait_for(asyncio.gather(*calls), 2)
        assert events == ["a", "i", "v", "b"]
        assert production.scheduler().snapshot() == {"concurrency": 1, "active": None, "waiting": []}

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_active", [False, True])
def test_cancellation_removes_waiters_and_keeps_the_next_job_available(cancel_active):
    async def exercise():
        queue = production.JobQueue("audio")
        for identifier in ("a", "b", "c"):
            queue.put_nowait(identifier)
        entered, finish = asyncio.Event(), asyncio.Event()
        seen = []

        async def process(identifier):
            seen.append(identifier)
            if identifier == "a":
                entered.set()
                await finish.wait()

        calls = [asyncio.create_task(queue.run(identifier, process)) for identifier in ("a", "b", "c")]
        await entered.wait()
        snapshot = production.scheduler().snapshot()
        assert snapshot["active"] == {"kind": "audio", "id": "a"}
        assert [(j["id"], j["position"]) for j in snapshot["waiting"]] == [("b", 1), ("c", 2)]
        calls[0 if cancel_active else 1].cancel()
        finish.set()
        await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 2)
        assert seen == (["a", "b", "c"] if cancel_active else ["a", "c"])
        assert not production.scheduler().waiting

    asyncio.run(exercise())


def test_cancel_before_consumer_starts_and_shutdown_release_all_reservations():
    async def exercise():
        queue = production.JobQueue("video")
        queue.put_nowait("cancelled")
        queue.put_nowait("next")
        production.discard(queue, "cancelled")
        seen = []

        async def process(identifier):
            seen.append(identifier)

        await queue.run("next", process)
        await queue.run("cancelled", process)
        assert seen == ["next"]
        queue.put_nowait("interrupted")
        production.close(queue)
        assert not production.scheduler().waiting

    asyncio.run(exercise())


def test_failures_release_admission_and_do_not_poison_later_jobs():
    async def exercise():
        queue = production.JobQueue("video")
        queue.put_nowait("bad")
        queue.put_nowait("good")
        seen = []

        async def process(identifier):
            seen.append(identifier)
            if identifier == "bad":
                raise ValueError("broken manifest")

        with pytest.raises(ValueError, match="broken manifest"):
            await queue.run("bad", process)
        await asyncio.wait_for(queue.run("good", process), 1)
        assert seen == ["bad", "good"]
        assert not production.scheduler().waiting

    asyncio.run(exercise())


def test_cancelled_job_keeps_admission_until_model_cleanup_completes(monkeypatch):
    async def exercise():
        entered, cleanup, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []

        @asynccontextmanager
        async def model_lease(kind, _preset):
            try:
                events.append(kind)
                yield
            finally:
                if kind == "image":
                    cleanup.set()
                    await finish.wait()

        monkeypatch.setattr(gpu, "_model_lease", model_lease)

        async def image():
            async with gpu.lease("image"):
                entered.set()
                await asyncio.Event().wait()

        async def speech():
            async with gpu.lease("speech"):
                pass

        image_task = asyncio.create_task(image())
        await entered.wait()
        speech_task = asyncio.create_task(speech())
        image_task.cancel()
        await cleanup.wait()
        assert events == ["image"]
        assert production.scheduler().snapshot()["active"]["kind"] == "image"
        finish.set()
        await asyncio.wait_for(asyncio.gather(image_task, speech_task, return_exceptions=True), 2)
        assert events == ["image", "speech"]
        assert not production.scheduler().waiting

    asyncio.run(exercise())


def test_lifespan_recovers_audio_and_video_in_global_timestamp_order(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "audio")
    monkeypatch.setattr(video, "ROOT", tmp_path / "video")
    monkeypatch.setattr(main, "JOB_WORKERS", 3)
    monkeypatch.setattr(gpu, "initialize", lambda: None)
    # IDs sort in reverse creation order; video was submitted before all audio.
    records = [("video", "v", "running", 1), ("audio", "z", "running", 2),
               ("audio", "a", "queued", 3), ("video", "cancelled", "cancelled", 0)]
    for kind, identifier, status, second in records:
        folder = (main.OUTPUT_DIR if kind == "audio" else video.ROOT / "exports") / identifier
        folder.mkdir(parents=True)
        (folder / "job.json").write_text(json.dumps({"id": identifier, "status": status, "chunks": [],
            "created_at": f"2026-10-16T10:00:0{second}+00:00"}))
    seen = []
    done = asyncio.Event()

    async def process(identifier):
        seen.append(identifier)
        await asyncio.sleep(0)
        if len(seen) == 3:
            done.set()

    monkeypatch.setattr(main, "process_generation_job", process)
    monkeypatch.setattr(video, "process_job", process)

    async def exercise():
        async with main.application_lifespan(main.app):
            await asyncio.wait_for(done.wait(), 2)
            assert seen == ["v", "z", "a"]
        assert not production.scheduler().waiting
        cancelled = video.read(video.ROOT / "exports/cancelled/job.json")
        assert cancelled["status"] == "cancelled"

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["local", "openai"])
def test_image_endpoint_waits_behind_audio_and_queue_status_remains_responsive(monkeypatch, provider):
    monkeypatch.setattr(main, "CLIP_IMAGE_PROVIDER", provider)
    monkeypatch.setattr(main, "OPENAI_API_KEY", "test")
    monkeypatch.setattr(gpu, "enabled", False)
    called = []
    original = httpx.AsyncClient

    def respond(request):
        called.append(request.url.path)
        return httpx.Response(200, json={"data": [{"b64_json": "iVBORw0KGgo="}]})

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))

    async def exercise():
        queue = production.JobQueue("audio")
        queue.put_nowait("episode")
        entered, finish = asyncio.Event(), asyncio.Event()

        async def audio(_identifier):
            entered.set()
            await finish.wait()

        task = asyncio.create_task(queue.run("episode", audio))
        await entered.wait()
        # Use ASGI to verify FastAPI's decorated form signature and response contract.
        async with original(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            image = asyncio.create_task(client.post("/api/images/generations", data={"prompt": "A city"}))
            await asyncio.sleep(0)
            response = await client.get("/api/production-queue")
            assert response.status_code == 200
            status = response.json()
            assert status["active"]["id"] == "episode"
            assert status["waiting"] == [{"kind": "image", "id": "request", "position": 1}]
            assert not called
            finish.set()
            result = await asyncio.wait_for(image, 2)
            assert result.status_code == 200
            assert result.headers["content-type"] == "image/png"
        await task
        assert len(called) == 1

    asyncio.run(exercise())
