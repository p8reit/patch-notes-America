import asyncio
import copy
import json

import httpx
import pytest
from fastapi import HTTPException

from app import gpu, main, video, video_automation as automation


class Result:
    status_code = 200
    is_success = True
    def __init__(self, body): self.body = body
    def json(self): return self.body
    def raise_for_status(self): pass


def test_free_search_is_bounded_cached_and_does_not_save_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ROOT", tmp_path)
    monkeypatch.setenv("PIXABAY_API_KEY", "private-key")
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url,**kwargs):
            calls.append(kwargs)
            return Result({"hits":[{"id":1,"user":"Author","pageURL":"https://pixabay.com/videos/id-1/","duration":10,
                                    "videos":{"large":{"url":"https://cdn.pixabay.com/video/example.mp4","width":1920,"height":1080}}}]})
    monkeypatch.setattr(automation.httpx,"AsyncClient",Client)
    async def run():
        first=await automation.search({"query":"housing"})
        assert await automation.search({"query":"housing"}) == first
        assert first["candidates"][0]["creator"] == "Author"
    asyncio.run(run())
    assert len(calls)==1 and calls[0]["params"]["per_page"]==10
    assert "private-key" not in next((tmp_path/"search").glob("*.json")).read_text()


@pytest.mark.parametrize("url",["http://cdn.pixabay.com/a","https://localhost/a","https://cdn.pixabay.com.evil/a","https://user@cdn.pixabay.com/a",
                               "https://cdn.pixabay.com:bad/a","https://[invalid/a",None])
def test_stock_cdn_allowlist_rejects_unsafe_urls(url):
    assert not automation.allowed_download(url)


def test_local_planner_preserves_locks_times_and_rolls_back_invalid_result(tmp_path, monkeypatch):
    monkeypatch.setattr(video,"ROOT",tmp_path)
    project={"id":"project","revision":1,"scenes":[
        {"id":"locked","locked":True,"text":"Keep this","start":0,"end":8,"prompt":"Approved"},
        {"id":"edit","locked":False,"text":"Housing","start":8,"end":16,"prompt":"Draft"}]}
    video.atomic(video.project_path("project"),project)
    invalid=False
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,**kwargs):
            assert url==main.LOCAL_AI_URL
            assert kwargs["json"]["keep_alive"]==0
            assert "do not force illustration" in kwargs["json"]["messages"][0]["content"]
            return Result({"message":{"content":json.dumps({"scenes":[{"id":"wrong" if invalid else "edit","intent":"Neighborhood","query":"houses","prompt":"Warm housing"}]})}})
    monkeypatch.setattr(automation.httpx,"AsyncClient",Client)
    result=asyncio.run(automation.plan("project"))
    assert result["scenes"][0]==project["scenes"][0]
    assert result["scenes"][1]["start"]==8 and result["scenes"][1]["prompt"]=="Warm housing"
    before=video.project_path("project").read_bytes()
    invalid=True
    with pytest.raises(HTTPException): asyncio.run(automation.plan("project"))
    assert video.project_path("project").read_bytes()==before


def test_gpu_handoff_serializes_nested_leases_and_restores_speech_after_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(gpu,"enabled",True)
    monkeypatch.setattr(gpu,"token_file",tmp_path/"control.key")
    monkeypatch.setattr(gpu,"lock",asyncio.Lock())
    gpu.initialize()
    calls=[]
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url): return Result({"models":[]})
        async def post(self,url,**kwargs):
            assert kwargs["headers"]["Authorization"].startswith("Bearer ")
            calls.append(url)
            return Result({})
    monkeypatch.setattr(gpu.httpx,"AsyncClient",Client)
    async def run():
        with pytest.raises(ValueError):
            async with gpu.lease("image"):
                count=len(calls)
                async with gpu.lease("image"): assert len(calls)==count
                raise ValueError("failed image")
    asyncio.run(run())
    assert calls[0].endswith("/models/unload")
    assert calls[-1]==main.CHATTERBOX_URL+"/models/load"
    assert gpu.owner.get() is None and gpu.state["active"]=="idle"
    assert gpu.token_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("provider", ["local", "openai"])
def test_batch_images_have_durable_progress_and_skip_locked_or_selected_scenes(tmp_path,monkeypatch,provider):
    monkeypatch.setattr(video,"ROOT",tmp_path)
    project={"id":"project","revision":1,"image_provider":provider,"scenes":[
        {"id":"locked","locked":True,"asset_id":None},
        {"id":"footage","locked":False,"asset_id":"selected"},
        {"id":"missing","locked":False,"asset_id":None},
        {"id":"prepared-footage","locked":False,"asset_id":None,"media_type":"broll"}]}
    video.atomic(video.project_path("project"),project)
    calls=[]
    async def image(project_id,body):
        calls.append(body["scene_id"])
        current=video.read(video.project_path(project_id))
        current["scenes"][2]["asset_id"]="created"
        current["revision"]+=1
        video.atomic(video.project_path(project_id),current)
        return current
    monkeypatch.setattr(video,"scene_image",image)
    from contextlib import asynccontextmanager
    leases=[]
    @asynccontextmanager
    async def lease(kind):
        leases.append(kind)
        yield
    monkeypatch.setattr(gpu,"lease",lease)
    async def run():
        monkeypatch.setattr(video,"queue",asyncio.Queue())
        job=await automation.batch_images("project")
        await video.process_job(job["id"])
        result=await video.job_status(job["id"])
        assert result["status"]=="complete" and result["done"]==1
    asyncio.run(run())
    assert calls==["missing"]
    assert leases == (["image"] if provider == "local" else [])


def test_build_reuses_one_prepared_clip_across_split_scenes_and_repairs_missing_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ROOT", tmp_path)
    candidate = {"url": "https://cdn.pixabay.com/video/clip.mp4"}
    path = video.child(tmp_path / "stock-selections", "selected") / "selection.json"
    video.atomic(path, {"candidate": candidate, "retrieved": 123, "asset_id": "missing-cache"})
    other_path = video.child(tmp_path / "stock-selections", "other-selection") / "selection.json"
    video.atomic(other_path, {"candidate": candidate, "retrieved": 123})
    project = {"revision": 1, "scenes": [
        {"id": "left", "asset_id": None, "media_type": "broll", "stock_id": "selected"},
        {"id": "right", "asset_id": None, "media_type": "broll", "stock_id": "other-selection"}]}
    video.atomic(video.project_path("project"), project)
    calls = []
    async def download(*args):
        calls.append(args)
        folder = video.child(tmp_path / "assets", "clip")
        folder.mkdir(parents=True)
        media = folder / "original.mp4"
        media.write_bytes(b"clip")
        item = {"id": "clip", "file": media.name, "hash": video.digest(media), "kind": "broll"}
        video.atomic(folder / "source.json", item)
        return item
    monkeypatch.setattr(automation, "download_candidate", download)
    job = {"project": project, "project_id": "project", "revision": 1}
    checkpoints = []
    asyncio.run(automation.prepare_build(job, lambda *values: checkpoints.append(values)))
    assert len(calls) == 1
    assert [s["asset_id"] for s in project["scenes"]] == ["clip", "clip"]
    assert video.read(path)["asset_id"] == "clip"
    assert video.read(other_path)["asset_id"] == "clip"
    assert checkpoints[-1] == (2, 2, "assets ready")


@pytest.mark.parametrize("provider", ["pixabay", "pexels"])
def test_expanded_search_paginates_and_separates_cache_entries(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(video, "ROOT", tmp_path)
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "private-key")
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            calls.append(kwargs)
            page = kwargs["params"]["page"]
            rows = [{"id": page*100+i, "user": {"name": "Creator"} if provider == "pexels" else "Creator",
                     "pageURL": "https://pixabay.com/videos/example/", "url": "https://www.pexels.com/video/example/",
                     "duration": 12,
                     "videos": {"large": {"url": "https://cdn.pixabay.com/video/example.mp4", "width": 1920, "height": 1080}},
                     "video_files": [{"link": "https://videos.pexels.com/example.mp4", "file_type": "video/mp4", "width": 1920, "height": 1080}]}
                    for i in range(35)]
            return Result({"hits" if provider == "pixabay" else "videos": rows})
    monkeypatch.setattr(automation.httpx, "AsyncClient", Client)
    async def run():
        default = await automation.search({"provider": provider, "query": "housing"})
        expanded = await automation.search({"provider": provider, "query": "housing", "per_page": 30})
        second = await automation.search({"provider": provider, "query": "housing", "per_page": 30, "page": 2})
        assert len(default["candidates"]) == 10
        assert len(expanded["candidates"]) == len(second["candidates"]) == 30
        assert len({default["id"], expanded["id"], second["id"]}) == 3
        assert second["candidates"][0]["id"] == "200"
        assert await automation.search({"provider": provider, "query": "housing", "per_page": 30, "page": 2}) == second
    asyncio.run(run())
    assert len(calls) == 3
    assert [c["params"]["per_page"] for c in calls] == [10, 30, 30]
    assert all("private-key" not in path.read_text() for path in (tmp_path / "search").glob("*.json"))


@pytest.mark.parametrize("bounds", [{"page": 0}, {"page": 3}, {"page": True}, {"per_page": 31}, {"per_page": "30"}])
def test_stock_search_rejects_unbounded_pagination(bounds):
    with pytest.raises(HTTPException) as error:
        asyncio.run(automation.search({"query": "housing", **bounds}))
    assert error.value.status_code == 400
