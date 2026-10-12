import asyncio
import base64
import copy
import json
import subprocess
from pathlib import Path

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, video, video_automation as automation


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
                 lambda p: p["scenes"][0].update(media_type=[]),
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


@pytest.mark.parametrize("aspect,dimensions",[("horizontal",(320,180)),("vertical",(180,320)),("square",(180,180))])
def test_real_format_exports_keep_audio_and_have_correct_dimensions(tmp_path,monkeypatch,aspect,dimensions):
    monkeypatch.setattr(video,"ROOT",tmp_path/"video")
    monkeypatch.setattr(main,"OUTPUT_DIR",tmp_path/"audio")
    monkeypatch.setitem(video.ASPECTS,aspect,dimensions)
    folder=main.OUTPUT_DIR/"episode"
    folder.mkdir(parents=True)
    def ffmpeg(*args):
        subprocess.run(["ffmpeg","-y","-v","error",*map(str,args)],check=True)
    ffmpeg("-f","lavfi","-i","sine=duration=8",folder/"episode.mp3")
    (folder/"metadata.json").write_text(json.dumps({"video_timeline":[{"start":0,"end":8,"text":"A format proof."}]}))
    footage=tmp_path/"clip.mp4";image=tmp_path/"still.png"
    ffmpeg("-f","lavfi","-i","color=c=blue:s=160x90:d=5",footage)
    ffmpeg("-f","lavfi","-i","color=c=orange:s=160x90","-frames:v","1","-threads","1",image)
    project=make_project(TestClient(main.app))
    broll=asyncio.run(video.store_asset(footage,"broll",{"license":"CC0"}))
    illustration=asyncio.run(video.store_asset(image,"image",{"provider":"local"}))
    left=copy.deepcopy(project["scenes"][0]);left.update(end=4,asset_id=broll["id"])
    right=copy.deepcopy(left);right.update(id="other",start=4,end=project["duration"],asset_id=illustration["id"],format_crops={aspect:{"x":.7,"y":.4}})
    project["scenes"]=[left,right]
    destination=video.ROOT/"exports"/aspect
    asyncio.run(video.render(project,destination,0,project["duration"],aspect,False,captions="large"))
    info=video.probe(destination/"episode.mp4")
    stream=next(s for s in info["streams"] if s["codec_type"]=="video")
    assert (stream["width"],stream["height"])==dimensions
    assert abs(float(info["format"]["duration"])-project["duration"])<.15
    assert video.digest(folder/"episode.mp3")==project["audio_hash"]


def test_variants_and_image_settings_round_trip_without_changing_source(studio):
    project=make_project(studio)
    project["image_preset"]="detail"
    project["scenes"][0].update(seed=123,format_crops={"vertical":{"x":.8,"y":.5}})
    response=studio.put('/api/video/projects/'+project["id"],json=project)
    assert response.status_code==200
    restored=response.json()
    assert restored["image_preset"]=="detail" and restored["scenes"][0]["seed"]==123
    assert restored["scenes"][0]["format_crops"]["vertical"]["x"]==.8
    restored["scenes"][0]["seed"]=-1
    assert studio.put('/api/video/projects/'+project["id"],json=restored).status_code==400


def test_gpu_cleanup_failure_cannot_revive_cancelled_job(studio,monkeypatch):
    project=make_project(studio)
    entered=asyncio.Event()
    async def cleanup_failure(*args,**kwargs):
        entered.set()
        try: await asyncio.Event().wait()
        finally: raise HTTPException(503,"Model finishing its current step")
    monkeypatch.setattr(video,"render",cleanup_failure)
    folder=video.child(video.ROOT/"exports","cancel-gpu")
    video.atomic(folder/"job.json",{"id":"cancel-gpu","project":project,"start":0,"end":16,"aspect":"horizontal","preview":True,"status":"queued"})
    async def run():
        task=asyncio.create_task(video.process_job("cancel-gpu"))
        await entered.wait()
        job=video.read(folder/"job.json");job["status"]="cancelled";video.atomic(folder/"job.json",job)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        assert video.read(folder/"job.json")["status"]=="cancelled"
    asyncio.run(run())


def test_corrupt_video_job_does_not_prevent_audio_or_video_startup(studio):
    folder=video.child(video.ROOT/"exports","damaged")
    folder.mkdir(parents=True)
    (folder/"job.json").write_text("{invalid")
    async def run():
        await video.start_workers()
        try:
            assert video.queue.empty()
            records=await video.jobs()
            assert records["jobs"][0]["status"]=="corrupt"
        finally: await video.stop_workers()
    asyncio.run(run())


def prepared_project(client):
    project = make_project(client)
    candidate = {"id": "clip", "provider": "pixabay", "url": "https://cdn.pixabay.com/video/clip.mp4",
                 "source_url": "https://pixabay.com/videos/clip/", "creator": "Author", "license": "Free stock",
                 "duration": 60, "width": 1920, "height": 1080}
    video.atomic(video.ROOT / "search/search.json", {"created": 123, "candidates": [candidate]})
    response = client.post(f"/api/video/projects/{project['id']}/scenes/{project['scenes'][0]['id']}/stock",
                           json={"revision": project["revision"], "search_id": "search", "candidate_id": "clip"})
    assert response.status_code == 200, response.text
    return response.json()


def test_prepare_stock_is_revision_checked_durable_and_does_not_download(studio, monkeypatch):
    async def unexpected(*args):
        raise AssertionError("Preparing must not download")
    monkeypatch.setattr(automation, "download_candidate", unexpected)
    project = prepared_project(studio)
    scene = project["scenes"][0]
    assert scene["media_type"] == "broll" and scene["asset_id"] is None and scene["stock_id"]
    assert not list((video.ROOT / "assets").glob("*"))
    video.atomic(video.ROOT / "search/search.json", {"created": 999, "candidates": []})
    scene["stock"]["url"] = "https://attacker.invalid/clip.mp4"
    saved = studio.put(f"/api/video/projects/{project['id']}", json=project)
    assert saved.status_code == 200
    assert saved.json()["scenes"][0]["stock"]["url"] == "https://cdn.pixabay.com/video/clip.mp4"
    stale = studio.post(f"/api/video/projects/{project['id']}/scenes/{scene['id']}/stock",
                        json={"revision": 1, "search_id": "search", "candidate_id": "clip"})
    assert stale.status_code == 409


def test_scene_source_round_trip_supports_legacy_assets_and_rejects_mismatches(studio):
    project = make_project(studio)
    footage = fake_asset("broll")
    legacy = copy.deepcopy(project)
    legacy["scenes"][0].pop("media_type")
    legacy["scenes"][0]["asset_id"] = footage["id"]
    saved = studio.put(f"/api/video/projects/{project['id']}", json=legacy)
    assert saved.status_code == 200
    current = saved.json()
    assert [s["media_type"] for s in current["scenes"]] == ["broll", "image"]
    current["scenes"][0]["media_type"] = "image"
    assert studio.put(f"/api/video/projects/{project['id']}", json=current).status_code == 400


@pytest.mark.parametrize("edit,message", [
    (lambda p: p["scenes"][0].update(stock_id=None), "find and select B-roll"),
    (lambda p: p["scenes"][1].update(locked=True), "unlock"),
    (lambda p: p["scenes"][0].update(trim=59), "too short"),
    (lambda p: p["scenes"][0].update(media_type="image", stock_id=None), "both local images and B-roll"),
])
def test_build_rejects_incomplete_plans_before_enqueueing(studio, monkeypatch, edit, message):
    project = prepared_project(studio)
    edit(project)
    video.atomic(video.project_path(project["id"]), project)
    async def run():
        monkeypatch.setattr(video, "queue", asyncio.Queue())
        with pytest.raises(HTTPException, match=message):
            await video.build(project["id"], {})
        assert video.queue.empty()
        assert not list((video.ROOT / "exports").glob("*/job.json"))
    asyncio.run(run())


@pytest.mark.parametrize("edit_during_build", [False, True])
def test_build_prepares_then_renders_frozen_plan_and_preserves_later_edits(studio, monkeypatch, edit_during_build):
    project = prepared_project(studio)
    calls = []
    footage, image = fake_asset("broll"), fake_asset("image")
    image["source"]["seed"] = 42
    async def download(candidate, retrieved):
        calls.append("footage")
        assert candidate["id"] == "clip" and retrieved == 123
        return footage
    async def generate(snapshot, scene):
        calls.append("image")
        assert snapshot["style"] == project["style"]
        if edit_during_build:
            current = video.read(video.project_path(project["id"]))
            current["style"] = "New direction"
            current["revision"] += 1
            video.atomic(video.project_path(project["id"]), current)
        return image
    async def render(snapshot, folder, start, end, aspect, preview, checkpoint, encoder, captions):
        calls.append("render")
        assert snapshot["style"] == project["style"]
        assert [s["asset_id"] for s in snapshot["scenes"]] == [footage["id"], image["id"]]
        assert (start, end, aspect, preview, encoder, captions) == (0, 16, "vertical", False, "libx264", "large")
    monkeypatch.setattr(automation, "download_candidate", download)
    monkeypatch.setattr(video, "generate_scene_asset", generate)
    monkeypatch.setattr(video, "render", render)
    async def run():
        monkeypatch.setattr(video, "queue", asyncio.Queue())
        job = await video.build(project["id"], {"aspect": "vertical", "captions": "large"})
        assert calls == [] and job["status"] == "queued"
        assert await video.queue.get() == job["id"]
        video.queue.task_done()
        await video.process_job(job["id"])
        result = await video.job_status(job["id"])
        assert result["status"] == "complete" and result["download_url"].endswith("/download")
        current = video.read(video.project_path(project["id"]))
        if edit_during_build:
            assert current["style"] == "New direction" and current["scenes"][1]["asset_id"] is None
        else:
            assert current["scenes"][1]["asset_id"] == image["id"] and current["scenes"][1]["seed"] == 42
            assert current["revision"] == project["revision"] + 1
        assert (main.OUTPUT_DIR / "episode/episode.mp3").read_bytes() == b"locked audio"
    asyncio.run(run())
    assert calls == ["footage", "image", "render"]


@pytest.mark.parametrize("cancel", [False, True])
def test_build_restart_reuses_assets_and_cancel_stays_terminal(studio, monkeypatch, cancel):
    project = prepared_project(studio)
    entered, rendered = asyncio.Event(), asyncio.Event()
    calls = []
    footage, image = fake_asset("broll"), fake_asset("image")
    image["source"]["seed"] = 7
    async def download(*args):
        calls.append("footage")
        return footage
    async def generate(*args):
        calls.append("image")
        if calls.count("image") == 1:
            entered.set()
            await asyncio.Event().wait()
        return image
    async def render(*args):
        rendered.set()
    monkeypatch.setattr(automation, "download_candidate", download)
    monkeypatch.setattr(video, "generate_scene_asset", generate)
    monkeypatch.setattr(video, "render", render)
    async def run():
        await video.start_workers()
        try:
            job = await video.build(project["id"], {})
            await asyncio.wait_for(entered.wait(), 2)
            progress = await video.job_status(job["id"])
            assert (progress["done"], progress["total"], progress["phase"]) == (1, 2, "generating local images")
            if cancel:
                assert (await video.cancel(job["id"]))["status"] == "cancelled"
            await video.stop_workers()
            persisted = video.read(video.ROOT / "exports" / job["id"] / "job.json")
            assert persisted["project"]["scenes"][0]["asset_id"] == footage["id"]
            await video.start_workers()
            await asyncio.wait_for(video.queue.join(), 2)
            assert (await video.job_status(job["id"]))["status"] == ("cancelled" if cancel else "complete")
            assert rendered.is_set() == (not cancel)
            assert calls.count("footage") == 1
            assert calls.count("image") == (1 if cancel else 2)
        finally:
            await video.stop_workers()
    asyncio.run(run())


def test_build_external_failure_keeps_prepared_assets_and_does_not_render(studio, monkeypatch):
    project = prepared_project(studio)
    async def download(*args): return fake_asset("broll")
    async def generate(*args): raise HTTPException(502, "Local image creation failed")
    async def render(*args): raise AssertionError("Incomplete assets must not render")
    monkeypatch.setattr(automation, "download_candidate", download)
    monkeypatch.setattr(video, "generate_scene_asset", generate)
    monkeypatch.setattr(video, "render", render)
    async def run():
        monkeypatch.setattr(video, "queue", asyncio.Queue())
        job = await video.build(project["id"], {})
        await video.process_job(job["id"])
        result = await video.job_status(job["id"])
        assert result["status"] == "failed" and result["error"] == "Local image creation failed"
        assert video.read(video.ROOT / "exports" / job["id"] / "job.json")["project"]["scenes"][0]["asset_id"]
    asyncio.run(run())


def test_real_ffmpeg_build_downloads_selected_stock_generates_local_images_and_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ROOT", tmp_path / "video")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "audio")
    monkeypatch.setitem(video.ASPECTS, "horizontal", (256, 144))
    folder = main.OUTPUT_DIR / "episode"
    folder.mkdir(parents=True)
    def ffmpeg(*args):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "1", *map(str, args)], check=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=16", folder / "episode.mp3")
    (folder / "metadata.json").write_text(json.dumps({"title": "Queued build", "video_timeline": [
        {"start": 0, "end": 16, "text": "A complete mixed media story."}]}))
    broll, image = tmp_path / "stock.mp4", tmp_path / "local.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=128x72:d=8:r=30", broll)
    ffmpeg("-f", "lavfi", "-i", "color=c=orange:s=128x72", "-frames:v", "1", "-threads", "1", image)
    client = TestClient(main.app)
    project = prepared_project(client)
    calls = []
    def respond(request):
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            assert str(request.url) == "https://cdn.pixabay.com/video/clip.mp4"
            return httpx.Response(200, content=broll.read_bytes())
        assert str(request.url) == main.LOCAL_IMAGES_URL
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image.read_bytes()).decode()}],
                                         "model_revision": "test-revision"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def run():
        await video.start_workers()
        try:
            queued = await video.build(project["id"], {})
            assert calls == []
            await asyncio.wait_for(video.queue.join(), 30)
            job = await video.job_status(queued["id"])
            assert job["status"] == "complete", job.get("error")
            destination = video.ROOT / "exports" / job["id"]
            info = video.probe(destination / "episode.mp4")
            assert abs(float(info["format"]["duration"]) - project["duration"]) < .15
            assert {"audio", "video"} <= {s["codec_type"] for s in info["streams"]}
            assert (destination / "captions.srt").is_file() and (destination / "credits.txt").is_file()
            assert "Author" in (destination / "credits.txt").read_text()
            assert video.digest(folder / "episode.mp3") == project["audio_hash"]
            assert len(calls) == len(project["scenes"])
            current = video.read(video.project_path(project["id"]))
            assert all(s["asset_id"] for s in current["scenes"])
            # A subsequent build reuses every prepared asset without model/network calls.
            reused = await video.build(project["id"], {})
            await asyncio.wait_for(video.queue.join(), 30)
            assert (await video.job_status(reused["id"]))["status"] == "complete"
            assert len(calls) == len(project["scenes"])
        finally:
            await video.stop_workers()
    asyncio.run(run())
