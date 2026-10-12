import asyncio
import base64
import copy
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, video


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ROOT", tmp_path / "video")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "audio")
    folder = main.OUTPUT_DIR / "episode"
    folder.mkdir(parents=True)
    (folder / "episode.mp3").write_bytes(b"locked audio")
    (folder / "metadata.json").write_text(json.dumps({"title": "A story", "duration": 16,
        "video_timeline": [{"id": "music", "text": "", "start": 0, "end": 2},
            {"id": "speech", "text": "Homes and household budgets.", "start": 2, "end": 16}]}))
    monkeypatch.setattr(video, "probe", lambda _path: {"format": {"duration": "16"},
                                                     "streams": [{"codec_type": "video", "width": 128, "height": 72}]})
    return TestClient(main.app)


def make_project(client):
    response = client.post("/api/video/projects", json={"episode_id": "episode"})
    assert response.status_code == 201, response.text
    return response.json()


def fake_asset(kind):
    identifier = kind + "-asset"
    folder = video.child(video.ROOT / "assets", identifier)
    folder.mkdir(parents=True)
    path = folder / ("original.png" if kind == "image" else "original.mp4")
    path.write_bytes(kind.encode())
    item = {"id": identifier, "kind": kind, "file": path.name, "hash": video.digest(path),
            "duration": 60, "source": {"provider": "local" if kind == "image" else "import", "license": "free"}}
    video.atomic(folder / "source.json", item)
    return item


def test_project_preserves_audio_and_music_timing_and_uses_optimistic_edits(studio):
    project = make_project(studio)
    assert project["transcript"][0]["start"] == 0
    assert project["transcript"][1]["start"] == 2
    assert project["duration"] == 16
    original = copy.deepcopy(project)
    project["scenes"][0]["prompt"] = "A warm neighborhood"
    saved = studio.put(f"/api/video/projects/{project['id']}", json=project)
    assert saved.status_code == 200
    assert saved.json()["revision"] == 2
    assert studio.put(f"/api/video/projects/{project['id']}", json=original).status_code == 409
    assert (main.OUTPUT_DIR / "episode/episode.mp3").read_bytes() == b"locked audio"
    assert studio.get("/api/video/episodes").json()["episodes"][0]["id"] == "episode"


def test_scene_validation_rejects_gaps_duplicates_and_missing_media(studio):
    original = make_project(studio)
    for edit in [lambda p: p["scenes"][0].update(end=1),
                 lambda p: p["scenes"][1].update(id=p["scenes"][0]["id"]),
                 lambda p: p["scenes"][0].update(asset_id="missing")]:
        project = copy.deepcopy(original)
        edit(project)
        assert studio.put(f"/api/video/projects/{project['id']}", json=project).status_code in {400, 404}


def test_export_requires_both_types_and_detects_changed_audio(studio):
    project = make_project(studio)
    with pytest.raises(HTTPException):
        video.render_inputs(project, 0, 16)
    image, broll = fake_asset("image"), fake_asset("broll")
    for scene in project["scenes"]:
        scene["asset_id"] = image["id"]
    with pytest.raises(HTTPException) as error:
        video.render_inputs(project, 0, 16)
    assert error.value.status_code == 409
    project["scenes"][0]["asset_id"] = broll["id"]
    assert len(video.render_inputs(project, 0, 16)) == 2
    (main.OUTPUT_DIR / "episode/episode.mp3").write_bytes(b"changed")
    with pytest.raises(HTTPException) as error:
        video.audio_file(project)
    assert error.value.status_code == 409


def test_media_import_records_provenance_and_rejects_traversal(studio):
    response = studio.post("/api/video/assets", data={"source_url": "https://example.com/footage", "creator": "Creator", "license": "CC0"},
                           files={"file": ("scene.mp4", b"video", "video/mp4")})
    assert response.status_code == 201
    item = response.json()
    assert item["source"]["license"] == "CC0"
    assert studio.get(item["url"]).content == b"video"
    for identifier in ["..", "../outside", "a/b", ""]:
        with pytest.raises(HTTPException):
            video.child(video.ROOT, identifier)


def test_scene_images_use_local_service_even_when_podcast_provider_is_openai(studio, monkeypatch):
    project = make_project(studio)
    png = b"\x89PNG\r\n\x1a\nmock"
    calls = []

    class Images:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, url, **kwargs):
            calls.append(url)
            return type("Result", (), {"raise_for_status": lambda self: None,
                "json": lambda self: {"data": [{"b64_json": base64.b64encode(png).decode()}]}})()

    monkeypatch.setattr(video.httpx, "AsyncClient", Images)
    response = studio.post(f"/api/video/projects/{project['id']}/images", json={"scene_id": project["scenes"][1]["id"]})
    assert response.status_code == 201
    assert calls == [main.LOCAL_IMAGES_URL]
    assert video.asset(response.json()["scenes"][1]["asset_id"])["source"]["provider"] == "local"


def test_real_ffmpeg_storytelling_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ROOT", tmp_path / "video")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "audio")
    monkeypatch.setitem(video.ASPECTS, "horizontal", (256, 144))
    folder = main.OUTPUT_DIR / "episode"
    folder.mkdir(parents=True)
    def ffmpeg(*args):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "1", *map(str,args)], check=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=60", folder / "episode.mp3")
    (folder / "metadata.json").write_text(json.dumps({"title": "Proof", "video_timeline": [
        {"start": 0, "end": 60, "text": "A complete documentary proof."}]}))
    broll = tmp_path / "broll.mp4"
    image = tmp_path / "image.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=128x72:d=8:r=30", broll)
    ffmpeg("-f", "lavfi", "-i", "color=c=orange:s=128x72", "-frames:v", "1", "-threads", "1", image)
    client = TestClient(main.app)
    project = make_project(client)
    footage = asyncio.run(video.store_asset(broll, "broll", {"license": "CC0", "creator": "test", "provider": "import"}))
    illustration = asyncio.run(video.store_asset(image, "image", {"provider": "local", "model": "test"}))
    for index, scene in enumerate(project["scenes"]):
        scene["asset_id"] = footage["id"] if index == 0 else illustration["id"]
    destination = video.ROOT / "exports/proof"
    asyncio.run(video.render(project, destination, 0, 60, "horizontal", True))
    info = video.probe(destination / "episode.mp4")
    assert abs(float(info["format"]["duration"]) - 60) < .15
    kinds = {s["codec_type"] for s in info["streams"]}
    assert {"audio", "video"} <= kinds <= {"audio", "video", "data"}
    assert info["chapters"]
    assert video.digest(folder / "episode.mp3") == project["audio_hash"]
    assert (destination / "captions.srt").is_file()
    assert (destination / "credits.txt").is_file()
    calls = []
    original = video.command
    async def count(args, cwd):
        calls.append(args)
        await original(args, cwd)
    monkeypatch.setattr(video, "command", count)
    asyncio.run(video.render(project, destination, 0, 60, "horizontal", True))
    assert len(calls) == 1, "Validated scene cache must avoid repeated scene rendering"


def test_video_job_cancellation_and_restart_recovery(studio, monkeypatch):
    project = make_project(studio)
    project["scenes"][0]["asset_id"] = fake_asset("broll")["id"]
    project["scenes"][1]["asset_id"] = fake_asset("image")["id"]
    entered = asyncio.Event()
    released = asyncio.Event()
    async def block(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
    monkeypatch.setattr(video, "render", block)
    async def run():
        video.atomic(video.project_path(project["id"]), project)
        await video.start_workers()
        try:
            job = await video.export(project["id"], {"start": 0, "end": 16})
            await asyncio.wait_for(entered.wait(), 2)
            cancelled = await video.cancel(job["id"])
            assert cancelled["status"] == "cancelled"
            assert released.is_set()
            await asyncio.wait_for(video.queue.join(), 2)
            assert not video.worker.done()
            assert (await video.cancel(job["id"]))["status"] == "cancelled"
            entered.clear()
            other = await video.export(project["id"], {"start": 0, "end": 16})
            await asyncio.wait_for(entered.wait(), 2)
            await video.stop_workers()
            assert (await video.job_status(other["id"]))["status"] == "queued"
            await video.start_workers()
            await asyncio.wait_for(entered.wait(), 2)
            assert (await video.job_status(job["id"]))["status"] == "cancelled"
        finally:
            await video.stop_workers()
    asyncio.run(run())


def test_video_export_freezes_project_and_does_not_block_audio(studio, monkeypatch):
    project = make_project(studio)
    project["scenes"][0]["asset_id"] = fake_asset("broll")["id"]
    project["scenes"][1]["asset_id"] = fake_asset("image")["id"]
    video.atomic(video.project_path(project["id"]), project)
    async def run():
        monkeypatch.setattr(video, "queue", asyncio.Queue())
        job = await video.export(project["id"], {"end":16})
        project["scenes"][0]["prompt"] = "Later edit"
        video.atomic(video.project_path(project["id"]), project)
        frozen = video.read(video.child(video.ROOT / "exports", job["id"]) / "job.json")
        assert frozen["project"]["scenes"][0]["prompt"] != "Later edit"
        assert frozen["revision"] == 1
    asyncio.run(run())


def test_subprocess_cancellation_terminates_renderer(tmp_path):
    async def run():
        task = asyncio.create_task(video.command(["/workspace/.venvs/patch-notes-America/bin/python", "-c", "import time;time.sleep(60)"], tmp_path))
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    asyncio.run(run())
