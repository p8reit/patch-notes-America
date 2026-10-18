"""Local video projects. Audio episodes remain immutable inputs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import signal
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from app import gpu, production_queue
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

ROOT = Path(os.getenv("VIDEO_DIR", Path(__file__).resolve().parent.parent / "data/video"))
router = APIRouter(prefix="/api/video", tags=["Video Studio"])
ASPECTS = {"horizontal": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}
queue: asyncio.Queue | None = None
worker: asyncio.Task | None = None
tasks: dict[str, asyncio.Task] = {}
logger = logging.getLogger(__name__)


def child(root: Path, identifier: str) -> Path:
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", identifier):
        raise HTTPException(400, "Invalid video identifier")
    result = root / identifier
    if not result.resolve().is_relative_to(root.resolve()):
        raise HTTPException(400, "Invalid media path")
    return result


def atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read(path: Path) -> dict:
    if not path.is_file():
        raise HTTPException(404, "Video project or asset not found")
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json", str(path)],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def number(value: Any, minimum: float = 0, maximum: float = 86400) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise HTTPException(400, "Video times and crop values must be finite numbers")
    if not minimum <= value <= maximum:
        raise HTTPException(400, "Video time or crop value is outside its allowed range")
    return float(value)


def project_path(identifier: str) -> Path:
    return child(ROOT / "projects", identifier) / "project.json"


def asset(identifier: str) -> dict:
    return read(child(ROOT / "assets", identifier) / "source.json")


def asset_file(item: dict) -> Path:
    return child(ROOT / "assets", item["id"]) / item["file"]


def audio_file(project: dict) -> Path:
    from app import main
    folder = (main.OUTPUT_DIR / project["episode_id"]).resolve()
    if folder.parent != main.OUTPUT_DIR.resolve():
        raise HTTPException(400, "Invalid episode path")
    path = folder / project["audio_file"]
    if not path.is_file() or digest(path) != project["audio_hash"]:
        raise HTTPException(409, "Source audio changed or is missing. Create a new video project.")
    return path


def scene_plan(transcript: list[dict], duration: float) -> list[dict]:
    scenes = []
    cursor = 0.0
    while cursor < duration:
        end = min(duration, cursor + 8)
        passages = [row for row in transcript if row["end"] > cursor and row["start"] < end]
        text = " ".join(row["text"] for row in passages)
        scenes.append({"id": uuid4().hex, "start": round(cursor, 3), "end": end if end == duration else round(end, 3),
                       "text": text, "intent": "Illustrate this passage", "query": text[:100],
                       "prompt": text[:1500] or "A cinematic establishing scene, no text",
                       "asset_id": None, "media_type": "image", "stock_id": None,
                       "trim": 0, "crop_x": 0.5, "crop_y": 0.5,
                       "motion": "still", "locked": False, "caption": text})
        cursor = end
    return scenes


def validate_scenes(scenes: Any, duration: float) -> list[dict]:
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 3000:
        raise HTTPException(400, "A video needs between 1 and 3000 scenes")
    cleaned, seen = [], set()
    cursor = 0.0
    for row in scenes:
        if not isinstance(row, dict):
            raise HTTPException(400, "Invalid scene")
        identifier = str(row.get("id", ""))
        child(ROOT, identifier)
        if identifier in seen:
            raise HTTPException(400, "Scene IDs must be unique")
        seen.add(identifier)
        start = number(row.get("start"), maximum=duration)
        # Older storyboards rounded the final boundary up to milliseconds.
        # Match the existing coverage tolerance and clamp to the exact audio end.
        end = min(duration, number(row.get("end"), maximum=duration + .002))
        if row is scenes[-1] and abs(end - duration) <= .002:
            end = duration
        if abs(start - cursor) > 0.002 or end <= start:
            raise HTTPException(400, "Scenes must cover the audio in order, without gaps or overlaps")
        chosen = row.get("asset_id")
        selected = asset(chosen) if chosen is not None else None
        media_type = row.get("media_type", selected["kind"] if selected else "image")
        if not isinstance(media_type, str) or media_type not in {"image", "broll"} or (selected and selected["kind"] != media_type):
            raise HTTPException(400, "Chosen media must match the scene's image or B-roll choice")
        stock_id = row.get("stock_id")
        stock = None
        if stock_id is not None:
            if media_type != "broll":
                raise HTTPException(400, "Only B-roll scenes can have prepared footage")
            stock = read(child(ROOT / "stock-selections", stock_id) / "selection.json")["candidate"]
        motion = row.get("motion", "still")
        if motion not in {"still", "zoom"}:
            raise HTTPException(400, "Motion must be still or zoom")
        cleaned.append({"id": identifier, "start": start, "end": end, "asset_id": chosen,
                        "media_type": media_type, "stock_id": stock_id, "stock": stock,
                        "trim": number(row.get("trim", 0)), "crop_x": number(row.get("crop_x", .5), maximum=1),
                        "crop_y": number(row.get("crop_y", .5), maximum=1), "motion": motion,
                        "locked": bool(row.get("locked", False)),
                        **{key: str(row.get(key, ""))[:limit] for key, limit in
                           [("text", 6000), ("caption", 6000), ("intent", 500), ("query", 100), ("prompt", 2000)]}})
        seed = row.get("seed")
        if seed is not None and (not isinstance(seed,int) or isinstance(seed,bool) or not 0 <= seed <= 2147483647):
            raise HTTPException(400,"Image seed must be an integer between 0 and 2147483647")
        cleaned[-1]["seed"] = seed
        crops = row.get("format_crops", {})
        if not isinstance(crops,dict) or any(key not in ASPECTS for key in crops):
            raise HTTPException(400,"Invalid format crop overrides")
        if any(not isinstance(value,dict) for value in crops.values()):
            raise HTTPException(400,"Invalid format crop values")
        cleaned[-1]["format_crops"] = {key:{"x":number(value.get("x",.5),maximum=1),"y":number(value.get("y",.5),maximum=1)}
                                       for key,value in crops.items() if isinstance(value,dict)}
        cursor = end
    if abs(cursor - duration) > .002:
        raise HTTPException(400, "Scenes must cover the full audio duration")
    return cleaned


@router.get("/episodes")
async def episodes():
    from app import main
    result = []
    for manifest in sorted(main.OUTPUT_DIR.glob("*/metadata.json"), reverse=True):
        try:
            job = read(manifest)
        except (OSError, ValueError):
            continue
        if list(manifest.parent.glob("*.mp3")):
            result.append({"id": manifest.parent.name, "title": job.get("title", manifest.parent.name)})
    return {"episodes": result}


@router.get("/projects")
async def projects():
    return {"projects": [read(path) for path in sorted((ROOT / "projects").glob("*/project.json"))]}


@router.post("/projects", status_code=201)
async def create_project(body: dict = Body(...)):
    from app import main
    episode_id = str(body.get("episode_id", ""))
    folder = (main.OUTPUT_DIR / episode_id).resolve()
    if folder.parent != main.OUTPUT_DIR.resolve():
        raise HTTPException(400, "Invalid episode ID")
    metadata = read(folder / "metadata.json")
    audio = sorted(folder.glob("*.mp3"))
    if not audio:
        raise HTTPException(409, "Finish podcast audio before creating video")
    info = await asyncio.to_thread(probe, audio[0])
    duration = number(float(info["format"]["duration"]))
    transcript = []
    for index, row in enumerate(metadata.get("video_timeline", metadata.get("utterances", []))):
        timing = row.get("timing", row)
        if "start" not in timing or "end" not in timing:
            raise HTTPException(409, "This episode has no complete audio timeline")
        start, end = number(timing["start"], maximum=duration + .3), number(timing["end"], maximum=duration + .3)
        if end < start:
            raise HTTPException(409, "Episode timing is invalid")
        transcript.append({"id": str(row.get("id", index)), "start": start, "end": min(end, duration),
                           "text": str(row.get("text", row.get("normalized_text", "")))})
    project = {"version": 1, "id": uuid4().hex, "revision": 1, "episode_id": episode_id,
               "title": metadata.get("title", "Video episode"), "audio_file": audio[0].name,
               "audio_hash": await asyncio.to_thread(digest, audio[0]), "duration": duration,
               "transcript": transcript, "style": "Documentary lighting, navy and amber, no lettering or logos",
               "image_provider": main.CLIP_IMAGE_PROVIDER, "image_quality": main.OPENAI_IMAGE_QUALITY,
               "image_preset": main.LOCAL_IMAGE_PRESET, "scenes": scene_plan(transcript, duration)}
    image_options(project)
    atomic(project_path(project["id"]), project)
    return project


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    return read(project_path(project_id))


@router.put("/projects/{project_id}")
async def save_project(project_id: str, body: dict = Body(...)):
    project = read(project_path(project_id))
    if body.get("revision") != project["revision"]:
        raise HTTPException(409, "Project changed. Reload before saving your edits.")
    project["scenes"] = validate_scenes(body.get("scenes"), project["duration"])
    project["style"] = str(body.get("style", project["style"]))[:1000]
    preset = body.get("image_preset",project.get("image_preset","draft"))
    if not isinstance(preset, str) or preset not in {"draft","detail"}:
        raise HTTPException(400,"Image preset must be draft or detail")
    provider = body.get("image_provider", project.get("image_provider", "local"))
    from app import main
    quality = body.get("image_quality", project.get("image_quality", main.OPENAI_IMAGE_QUALITY))
    if not isinstance(quality, str) or quality not in {"low", "medium", "high"}:
        raise HTTPException(400, "Hosted image quality must be low, medium, or high")
    main.image_settings(provider, quality, preset)
    project["image_provider"] = provider
    project["image_quality"] = quality
    project["image_preset"] = preset
    project["revision"] += 1
    atomic(project_path(project_id), project)
    return project


@router.get("/assets")
async def assets():
    return {"assets": [read(path) for path in sorted((ROOT / "assets").glob("*/source.json"))]}


async def store_asset(path: Path, kind: str, provenance: dict) -> dict:
    info = await asyncio.to_thread(probe, path)
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not videos or (kind == "broll" and not float(info.get("format", {}).get("duration", 0))):
        raise HTTPException(400, "Uploaded file is not valid video media")
    hashed = await asyncio.to_thread(digest, path)
    identifier = hashlib.sha256((hashed + json.dumps(provenance, sort_keys=True)).encode()).hexdigest()
    folder = child(ROOT / "assets", identifier)
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / ("original" + path.suffix)
    path.replace(destination)
    item = {"id": identifier, "kind": kind, "file": destination.name, "hash": hashed,
            "width": videos[0]["width"], "height": videos[0]["height"],
            "duration": float(info.get("format", {}).get("duration", 0)), "source": provenance,
            "url": f"/api/video/assets/{identifier}/media"}
    atomic(folder / "source.json", item)
    return item


@router.post("/assets", status_code=201)
async def import_asset(file: UploadFile = File(...), source_url: str = Form(...),
                       creator: str = Form(...), license: str = Form(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"} or not all(x.strip() for x in [source_url, creator, license]):
        raise HTTPException(400, "Import MP4/MOV/WebM with its source, creator, and license")
    temporary = ROOT / "imports" / (uuid4().hex + suffix)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        total = 0
        with temporary.open("wb") as stream:
            while data := await file.read(1024 * 1024):
                total += len(data)
                if total > 512 * 1024 * 1024:
                    raise HTTPException(413, "Footage exceeds 512 MB")
                await asyncio.to_thread(stream.write, data)
        return await store_asset(temporary, "broll", {"provider": "import", "url": source_url[:2000],
                                                       "creator": creator[:300], "license": license[:2000]})
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


@router.get("/assets/{asset_id}/media")
async def media(asset_id: str):
    item = asset(asset_id)
    return FileResponse(asset_file(item))


@router.post("/projects/{project_id}/images", status_code=201)
async def scene_image(project_id: str, body: dict = Body(...)):
    project = read(project_path(project_id))
    scene = next((s for s in project["scenes"] if s["id"] == body.get("scene_id")), None)
    if scene is None or scene["locked"]:
        raise HTTPException(409, "Choose an unlocked scene")
    if scene.get("media_type", "image") != "image":
        raise HTTPException(409, "Choose image for this scene before generating")
    revision = project["revision"]
    item = await generate_scene_asset(project, scene)
    current = read(project_path(project_id))
    if current["revision"] != revision:
        raise HTTPException(409, "Image saved to the library; project changed. Select it after reloading.")
    scene.update(asset_id=item["id"], media_type="image", stock_id=None, stock=None, seed=item["source"]["seed"])
    project["revision"] += 1
    atomic(project_path(project_id), project)
    return project


def image_options(project: dict) -> tuple[str, dict]:
    from app import main
    # Missing provider preserves the local-only behavior of legacy projects.
    provider = project.get("image_provider", "local")
    settings = main.image_settings(provider, project.get("image_quality"), project.get("image_preset", "draft"))
    return provider, settings


@asynccontextmanager
async def image_lease(project: dict):
    provider, settings = image_options(project)
    if provider == "local":
        async with gpu.lease("image", image_preset=settings["preset"]):
            yield
    else:
        yield


@production_queue.job("image")
async def generate_scene_asset(project: dict, scene: dict) -> dict:
    """Generate a snapshot asset; lease local CUDA only for local requests."""
    from app import main
    provider, settings = image_options(project)
    if provider == "openai" and not main.OPENAI_API_KEY:
        raise HTTPException(503, "OPENAI_API_KEY is not configured for video images")
    prompt = f"Visual style: {project['style']}. Scene: {scene['prompt']}. No text, logos or watermarks."
    seed = scene.get("seed")
    if provider == "local" and seed is None:
        seed = uuid4().int & 2147483647
    if provider == "openai":
        seed = None  # Hosted API does not support deterministic seeds.
    endpoint = main.LOCAL_IMAGES_URL if provider == "local" else main.OPENAI_IMAGES_URL
    headers = {"Content-Type": "application/json"}
    if provider == "openai":
        headers["Authorization"] = f"Bearer {main.OPENAI_API_KEY}"
    payload = {**settings, "prompt": prompt[:4000], "size": "1536x1024", "n": 1}
    if provider == "local":
        payload["seed"] = seed
    try:
        async with image_lease(project):
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
        body = response.json()
        data = base64.b64decode(body["data"][0]["b64_json"], validate=True)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(502, f"{provider} image creation failed. Check the configured image service and retry.") from exc
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 25 * 1024 * 1024:
        raise HTTPException(502, "Image service returned invalid PNG media")
    path = ROOT / "imports" / (uuid4().hex + ".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(data)
        item = await store_asset(path, "image", {"provider": provider, **settings,
                                "model": body.get("model", settings["model"]),
                                "prompt": prompt, "seed": seed,
                                "model_revision": body.get("model_revision", "unrecorded"),
                                **{key: body[key] for key in ("steps", "guidance", "width", "height") if key in body},
                                "license": "See the image provider/model terms"})
    finally:
        path.unlink(missing_ok=True)
    return item


def render_inputs(project: dict, start: float, end: float) -> list[dict]:
    selected = [s for s in project["scenes"] if s["end"] > start and s["start"] < end]
    if any(not s["asset_id"] for s in selected):
        raise HTTPException(409, "Choose media for every scene in the export range")
    kinds = {asset(s["asset_id"])["kind"] for s in selected}
    if kinds != {"broll", "image"}:
        raise HTTPException(409, "Storytelling exports require both B-roll and generated images")
    checked = set()
    for scene in selected:
        item = asset(scene["asset_id"])
        path = asset_file(item)
        if item["id"] not in checked:
            if not path.is_file() or digest(path) != item["hash"]:
                raise HTTPException(409, "Selected media changed or is missing")
            checked.add(item["id"])
        length = min(scene["end"], end) - max(scene["start"], start)
        if item["kind"] == "broll" and scene["trim"] + max(scene["start"], start) - scene["start"] + length > item["duration"] + .01:
            raise HTTPException(400, "B-roll is shorter than its scene; trim or split the scene")
    return selected


async def command(arguments: list[str], cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(*arguments, cwd=cwd, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        _, error = await process.communicate()
        if process.returncode:
            raise RuntimeError(error.decode(errors="replace")[-2000:])
    except asyncio.CancelledError:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        raise


@production_queue.job("video")
async def render(project: dict, destination: Path, start: float, end: float, aspect: str, preview: bool,
                 checkpoint: Any = None, encoder: str = "libx264", captions: str = "standard") -> None:
    scenes = await asyncio.to_thread(render_inputs, project, start, end)
    audio = await asyncio.to_thread(audio_file, project)
    width, height = ASPECTS[aspect]
    if preview:
        width, height = width // 2, height // 2
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, scene in enumerate(scenes):
        item = asset(scene["asset_id"])
        length = min(scene["end"], end) - max(scene["start"], start)
        inputs = ["-loop", "1", "-framerate", "30"] if item["kind"] == "image" else ["-ss", str(scene["trim"] + max(scene["start"], start) - scene["start"])]
        crop = scene.get("format_crops",{}).get(aspect,{"x":scene['crop_x'],"y":scene['crop_y']})
        scale = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}:(iw-ow)*{crop['x']}:(ih-oh)*{crop['y']},setsar=1,fps=30"
        if item["kind"] == "image" and scene["motion"] == "zoom":
            scale += f",zoompan=z='min(zoom+0.0003,1.08)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps=30"
        signature = {"renderer": 1, "asset": item["hash"], "inputs": inputs, "length": length, "filter": scale}
        key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        cache = child(ROOT / "cache", key)
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / "scene.mp4"
        valid = False
        if path.is_file():
            try:
                info = await asyncio.to_thread(probe, path)
                valid = abs(float(info["format"]["duration"]) - length) < .1
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        if not valid:
            temporary = cache / "scene.partial.mp4"
            try:
                await command(["ffmpeg", "-y", "-v", "error", *inputs, "-i", str(asset_file(item)), "-t", str(length),
                               "-vf", scale, "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(temporary)], destination)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        if checkpoint:
            checkpoint(index + 1, len(scenes), "preparing scenes")
        paths.append(path)
    (destination / "concat.txt").write_text("".join(f"file '{p.as_posix()}'\n" for p in paths))
    write_sidecars(project, scenes, destination, start, end)
    if checkpoint:
        checkpoint(len(scenes), len(scenes), "assembling video")
    caption_filter = ["-vf", "subtitles=captions.srt:force_style='Fontsize="+str(24 if captions=='large' else 18)+",Outline=2,MarginV=24'"] if captions!='none' and (destination / "captions.srt").read_text().strip() else []
    async def assemble():
        await command(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-ss", str(start),
                   "-i", str(audio), "-i", "chapters.ffmeta", "-map_metadata", "2", "-map", "0:v:0", "-map", "1:a:0",
                   "-t", str(end-start), *caption_filter, "-c:v", encoder, "-preset", "p4" if encoder=='h264_nvenc' else "veryfast", "-profile:v","baseline","-level:v","4.1", "-pix_fmt","yuv420p", "-c:a", "aac",
                   "-b:a", "192k", "-movflags", "+faststart", "episode.partial.mp4"], destination)
    if encoder == "h264_nvenc":
        async with gpu.lease("encoder"):
            await assemble()
    else:
        await assemble()
    (destination / "episode.partial.mp4").replace(destination / "episode.mp4")


def stamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    return f"{milliseconds//3600000:02}:{milliseconds//60000%60:02}:{milliseconds//1000%60:02},{milliseconds%1000:03}"


def write_sidecars(project: dict, scenes: list[dict], destination: Path, start: float, end: float) -> None:
    changed = [s for s in scenes if s.get("caption", "") != s.get("text", "")]
    rows = []
    for row in project["transcript"]:
        words = row["text"].split()
        count = max(1, math.ceil(len(words)/12))
        for index in range(count):
            a = row["start"] + (row["end"]-row["start"])*index/count
            b = row["start"] + (row["end"]-row["start"])*(index+1)/count
            fragments = [(a,b)]
            for scene in changed:
                fragments = [(left,right) for x,y in fragments for left,right in
                             [(x,min(y,scene["start"])),(max(x,scene["end"]),y)] if right>left]
            rows += [{"start":a,"end":b,"text":" ".join(words[index*12:(index+1)*12])} for a,b in fragments]
    rows += [{"start": s["start"], "end": s["end"], "text": s["caption"]} for s in changed]
    entries = []
    for row in sorted(rows, key=lambda r: r["start"]):
        a, b = max(start, row["start"]), min(end, row["end"])
        if b <= a or not row["text"].strip():
            continue
        words = row["text"].split()
        phrases = [" ".join(words[i:i+12]) for i in range(0, len(words), 12)]
        for index, phrase in enumerate(phrases):
            left, right = a + (b-a)*index/len(phrases), a + (b-a)*(index+1)/len(phrases)
            entries.append(f"{len(entries)+1}\n{stamp(left-start)} --> {stamp(right-start)}\n{phrase.replace('<', '').replace('>', '')}\n")
    (destination / "captions.srt").write_text("\n".join(entries))
    sources = {s["asset_id"]: asset(s["asset_id"])["source"] for s in scenes}
    (destination / "credits.txt").write_text("\n\n".join(json.dumps(source, indent=2) for source in sources.values()))
    chapters = [";FFMETADATA1"]
    for index, scene in enumerate(scenes):
        if index % 8:
            continue
        left = max(start, scene["start"])
        right = min(end, scenes[min(index+7, len(scenes)-1)]["end"])
        title = re.sub(r"[\\;=#\r\n]", " ", scene["intent"])
        chapters.append(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={round((left-start)*1000)}\nEND={round((right-start)*1000)}\ntitle={title}")
    (destination / "chapters.ffmeta").write_text("\n".join(chapters))


@router.post("/projects/{project_id}/exports", status_code=202)
async def export(project_id: str, body: dict = Body(...)):
    return await enqueue_export(project_id, body)


@router.post("/projects/{project_id}/build", status_code=202)
async def build(project_id: str, body: dict = Body(...)):
    return await enqueue_export(project_id, body, prepare=True)


async def enqueue_export(project_id: str, body: dict, prepare: bool = False) -> dict:
    project = read(project_path(project_id))
    start, end = number(body.get("start", 0), maximum=project["duration"]), number(body.get("end", project["duration"]), maximum=project["duration"])
    if end <= start:
        raise HTTPException(400, "Export end must follow its start")
    aspect, encoder, captions = body.get("aspect","horizontal"), body.get("encoder","libx264"), body.get("captions","standard")
    if aspect not in ASPECTS or encoder not in {"libx264","h264_nvenc"} or captions not in {"standard","large","none"}:
        raise HTTPException(400,"Invalid video format, encoder, or caption style")
    if queue is None:
        raise HTTPException(503, "Video worker is not ready")
    if prepare:
        from app.video_automation import validate_build
        automatic = body.get("auto_broll", False)
        if not isinstance(automatic, bool):
            raise HTTPException(400, "Automatic B-roll must be true or false")
        provider = body.get("provider", "pixabay")
        project["scenes"] = validate_scenes(project["scenes"], project["duration"])
        needs_selection = any(not s["locked"] and not s["asset_id"] and not s.get("stock_id") for s in project["scenes"])
        if automatic:
            if not isinstance(provider, str) or provider not in {"pixabay", "pexels"}:
                raise HTTPException(400, "Choose Pixabay or Pexels for automatic B-roll")
            if needs_selection and not os.getenv("PIXABAY_API_KEY" if provider == "pixabay" else "PEXELS_API_KEY"):
                raise HTTPException(400, "Automatic B-roll needs a configured Pixabay or Pexels API key; manual imported footage remains available")
        await asyncio.to_thread(validate_build, project, start, end, automatic)
    else:
        await asyncio.to_thread(render_inputs, project, start, end)
    await asyncio.to_thread(audio_file, project)
    identifier = uuid4().hex
    job = {"id": identifier, "project_id": project_id, "project": project, "revision": project["revision"],
           "type": "build" if prepare else "export",
           "start": start, "end": end, "aspect":aspect,"encoder":encoder,"captions":captions,"preview":bool(body.get("preview",not prepare)),
           "status": "queued", "phase": "queued", "done": 0, "total": 0, "error": None,
           "created_at": datetime.now(timezone.utc).isoformat()}
    if prepare and automatic:
        job.update(auto_broll=True, provider=provider, warnings=[])
    atomic(child(ROOT / "exports", identifier) / "job.json", job)
    await queue.put(identifier)
    return job


@router.get("/exports/{export_id}/download")
async def download(export_id: str):
    path = child(ROOT / "exports", export_id) / "episode.mp4"
    if not path.is_file():
        raise HTTPException(404, "Video export not found")
    return FileResponse(path, media_type="video/mp4", filename="episode.mp4")


@router.get("/jobs")
async def jobs():
    result = []
    for path in sorted((ROOT / "exports").glob("*/job.json"), key=lambda p:p.stat().st_mtime, reverse=True):
        try:
            result.append({k:v for k,v in read(path).items() if k!="project"})
        except (ValueError,OSError):
            result.append({"id":path.parent.name,"project_id":path.parent.name,"status":"corrupt",
                           "phase":"needs repair","done":0,"total":0,"error":"Video job manifest is unreadable. Retain its media and restore its manifest from a backup."})
    return {"jobs":result[:100]}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = read(child(ROOT / "exports", job_id) / "job.json")
    return {k: v for k, v in job.items() if k != "project"}


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    path = child(ROOT / "exports", job_id) / "job.json"
    job = read(path)
    if job["status"] not in {"queued", "running", "cancelled"}:
        raise HTTPException(409, "Only active jobs can be cancelled")
    job.update(status="cancelled", phase="cancelled")
    atomic(path, job)
    task = tasks.get(job_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    production_queue.discard(queue, job_id)
    return await job_status(job_id)


@router.get("/exports/{export_id}/sidecar/{name}")
async def sidecar(export_id: str, name: str):
    if name not in {"captions.srt", "credits.txt", "chapters.ffmeta"}:
        raise HTTPException(404, "Export sidecar not found")
    path = child(ROOT / "exports", export_id) / name
    if not path.is_file():
        raise HTTPException(404, "Export sidecar not found")
    return FileResponse(path, filename=name)


async def process_job(identifier: str) -> None:
    folder = child(ROOT / "exports", identifier)
    path = folder / "job.json"
    job = read(path)
    if job["status"] not in {"queued", "running"}:
        return
    def checkpoint(done: int, total: int, phase: str) -> None:
        job.update(done=done, total=total, phase=phase)
        atomic(path, job)
    job.update(status="running", error=None)
    atomic(path, job)
    try:
        if job.get("type") == "images":
            async with image_lease(job["project"]):
                scenes = [s for s in job["project"]["scenes"] if not s["locked"] and not s["asset_id"]
                          and s.get("media_type", "image") == "image"]
                for index, scene in enumerate(scenes):
                    if read(project_path(job["project_id"]))["revision"] != job["revision"]:
                        raise HTTPException(409, "Storyboard changed. Cancel/restart the image batch after reviewing edits.")
                    project = await scene_image(job["project_id"], {"scene_id":scene["id"]})
                    job.update(project=project,revision=project["revision"])
                    checkpoint(index+1,len(scenes),"generating images")
            job.update(status="complete",phase="images complete")
            return
        if job.get("type") == "build":
            from app.video_automation import prepare_build
            await asyncio.to_thread(audio_file, job["project"])
            if job.get("auto_broll"):
                from app.video_auto import automatic_visuals
                await automatic_visuals(job, checkpoint)
            await prepare_build(job, checkpoint)
        await render(job["project"], folder, job["start"], job["end"], job["aspect"], job["preview"], checkpoint,
                     job.get("encoder","libx264"),job.get("captions","standard"))
        job.update(status="complete", phase="complete", download_url=f"/api/video/exports/{identifier}/download")
    except asyncio.CancelledError:
        persisted = read(path)
        job["status"] = "cancelled" if persisted["status"] == "cancelled" else "queued"
        job["phase"] = job["status"]
        raise
    except Exception as exc:
        if asyncio.current_task().cancelling():
            job["status"] = "cancelled" if read(path)["status"] == "cancelled" else "queued"
            job["phase"] = job["status"]
            raise asyncio.CancelledError() from exc
        job.update(status="failed", error=exc.detail if isinstance(exc, HTTPException) else str(exc), phase="failed")
    finally:
        if read(path)["status"] == "cancelled":
            job.update(status="cancelled",phase="cancelled",error=None)
        (folder / "episode.partial.mp4").unlink(missing_ok=True)
        atomic(path, job)


async def consume() -> None:
    while True:
        identifier = await queue.get()
        task = asyncio.create_task(production_queue.run(queue, identifier, process_job))
        tasks[identifier] = task
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            logger.exception("Video worker could not read job %s; retaining remaining queue",identifier)
        finally:
            tasks.pop(identifier, None)
            queue.task_done()


async def start_workers() -> None:
    global queue, worker
    queue = production_queue.JobQueue("video")
    recovered = []
    for path in (ROOT / "exports").glob("*/job.json"):
        try:
            job = read(path)
        except (ValueError,OSError):
            logger.warning("Unreadable video job manifest: %s",path)
            continue
        recovered.append((path, job))
    recovered.sort(key=lambda item: production_queue.recovery_order(item[1].get("created_at"), item[0].parent.name))
    for path, job in recovered:
        if job["status"] in {"queued", "running"}:
            job.update(status="queued", phase="recovering")
            atomic(path, job)
            queue.enqueue(job["id"], job.get("created_at"))
    worker = asyncio.create_task(consume())


async def stop_workers() -> None:
    global queue, worker
    if worker:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    worker = None
    production_queue.close(queue)
    queue = None
