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


@pytest.mark.parametrize("url",["http://cdn.pixabay.com/a","https://localhost/a","https://cdn.pixabay.com.evil/a","https://user@cdn.pixabay.com/a"])
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


def test_batch_images_have_durable_progress_and_skip_locked_or_selected_scenes(tmp_path,monkeypatch):
    monkeypatch.setattr(video,"ROOT",tmp_path)
    project={"id":"project","revision":1,"scenes":[
        {"id":"locked","locked":True,"asset_id":None},
        {"id":"footage","locked":False,"asset_id":"selected"},
        {"id":"missing","locked":False,"asset_id":None}]}
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
    async def run():
        monkeypatch.setattr(video,"queue",asyncio.Queue())
        job=await automation.batch_images("project")
        await video.process_job(job["id"])
        result=await video.job_status(job["id"])
        assert result["status"]=="complete" and result["done"]==1
    asyncio.run(run())
    assert calls==["missing"]
