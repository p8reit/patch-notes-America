"""Local video projects. Audio episodes remain immutable inputs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

ROOT = Path(os.getenv("VIDEO_DIR", Path(__file__).resolve().parent.parent / "data/video"))
router = APIRouter(prefix="/api/video", tags=["Video Studio"])
ASPECTS = {"horizontal": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}


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
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
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
        scenes.append({"id": uuid4().hex, "start": round(cursor, 3), "end": round(end, 3),
                       "text": text, "intent": "Illustrate this passage", "query": text[:100],
                       "prompt": text[:1500] or "A cinematic establishing scene, no text",
                       "asset_id": None, "trim": 0, "crop_x": 0.5, "crop_y": 0.5,
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
        start, end = number(row.get("start"), maximum=duration), number(row.get("end"), maximum=duration)
        if abs(start - cursor) > 0.002 or end <= start:
            raise HTTPException(400, "Scenes must cover the audio in order, without gaps or overlaps")
        chosen = row.get("asset_id")
        if chosen is not None:
            asset(chosen)
        motion = row.get("motion", "still")
        if motion not in {"still", "zoom"}:
            raise HTTPException(400, "Motion must be still or zoom")
        cleaned.append({"id": identifier, "start": start, "end": end, "asset_id": chosen,
                        "trim": number(row.get("trim", 0)), "crop_x": number(row.get("crop_x", .5), maximum=1),
                        "crop_y": number(row.get("crop_y", .5), maximum=1), "motion": motion,
                        "locked": bool(row.get("locked", False)),
                        **{key: str(row.get(key, ""))[:limit] for key, limit in
                           [("text", 6000), ("caption", 6000), ("intent", 500), ("query", 100), ("prompt", 2000)]}})
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
               "scenes": scene_plan(transcript, duration)}
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
    from app import main
    project = read(project_path(project_id))
    scene = next((s for s in project["scenes"] if s["id"] == body.get("scene_id")), None)
    if scene is None or scene["locked"]:
        raise HTTPException(409, "Choose an unlocked scene")
    revision = project["revision"]
    prompt = f"{project['style']}. Illustrative editorial artwork. {scene['prompt']}. No text."
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
            response = await client.post(main.LOCAL_IMAGES_URL, json={"model": main.LOCAL_IMAGE_MODEL,
                             "prompt": prompt[:4000], "size": "1536x1024", "n": 1})
            response.raise_for_status()
        data = base64.b64decode(response.json()["data"][0]["b64_json"], validate=True)
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        raise HTTPException(502, "Local image creation failed. Check the local-image service and retry.") from exc
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 25 * 1024 * 1024:
        raise HTTPException(502, "Local image service returned invalid PNG media")
    path = ROOT / "imports" / (uuid4().hex + ".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(data)
        item = await store_asset(path, "image", {"provider": "local", "model": main.LOCAL_IMAGE_MODEL,
                                "prompt": prompt, "license": "See the installed model's license"})
    finally:
        path.unlink(missing_ok=True)
    current = read(project_path(project_id))
    if current["revision"] != revision:
        raise HTTPException(409, "Image saved to the library; project changed. Select it after reloading.")
    scene["asset_id"] = item["id"]
    project["revision"] += 1
    atomic(project_path(project_id), project)
    return project


def render_inputs(project: dict, start: float, end: float) -> list[dict]:
    selected = [s for s in project["scenes"] if s["end"] > start and s["start"] < end]
    if any(not s["asset_id"] for s in selected):
        raise HTTPException(409, "Choose media for every scene in the export range")
    kinds = {asset(s["asset_id"])["kind"] for s in selected}
    if kinds != {"broll", "image"}:
        raise HTTPException(409, "Storytelling exports require both B-roll and locally generated images")
    for scene in selected:
        item = asset(scene["asset_id"])
        path = asset_file(item)
        if not path.is_file() or digest(path) != item["hash"]:
            raise HTTPException(409, "Selected media changed or is missing")
        length = min(scene["end"], end) - max(scene["start"], start)
        if item["kind"] == "broll" and scene["trim"] + max(scene["start"], start) - scene["start"] + length > item["duration"] + .01:
            raise HTTPException(400, "B-roll is shorter than its scene; trim or split the scene")
    return selected


async def command(arguments: list[str], cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(*arguments, cwd=cwd, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await process.communicate()
        if process.returncode:
            raise RuntimeError(error.decode(errors="replace")[-2000:])
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


async def render(project: dict, destination: Path, start: float, end: float, aspect: str, preview: bool) -> None:
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
        scale = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}:(iw-ow)*{scene['crop_x']}:(ih-oh)*{scene['crop_y']},setsar=1,fps=30"
        if item["kind"] == "image" and scene["motion"] == "zoom":
            scale += f",zoompan=z='min(zoom+0.0003,1.08)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps=30"
        path = destination / f"scene-{index:04d}.mp4"
        await command(["ffmpeg", "-y", "-v", "error", *inputs, "-i", str(asset_file(item)), "-t", str(length),
                       "-vf", scale, "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(path)], destination)
        paths.append(path)
    (destination / "concat.txt").write_text("".join(f"file '{p.name}'\n" for p in paths))
    await command(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "1", "-i", "concat.txt", "-ss", str(start),
                   "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-t", str(end-start), "-c:v", "copy", "-c:a", "aac",
                   "-b:a", "192k", "-movflags", "+faststart", "episode.mp4"], destination)


@router.post("/projects/{project_id}/exports")
async def export(project_id: str, body: dict = Body(...)):
    project = read(project_path(project_id))
    start, end = number(body.get("start", 0), maximum=project["duration"]), number(body.get("end", project["duration"]), maximum=project["duration"])
    if not 0 < end-start <= 90:
        raise HTTPException(400, "Phase-one proof exports must be between 0 and 90 seconds")
    identifier = uuid4().hex
    await render(project, child(ROOT / "exports", identifier), start, end, "horizontal", bool(body.get("preview", True)))
    return {"id": identifier, "download_url": f"/api/video/exports/{identifier}/download"}


@router.get("/exports/{export_id}/download")
async def download(export_id: str):
    path = child(ROOT / "exports", export_id) / "episode.mp4"
    if not path.is_file():
        raise HTTPException(404, "Video export not found")
    return FileResponse(path, media_type="video/mp4", filename="episode.mp4")
