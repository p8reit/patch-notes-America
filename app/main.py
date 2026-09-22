from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.background import BackgroundTask

from app.audio_utils import (
    analyze_final_audio_silence,
    concatenate_wav_segments,
    is_speakable_text,
    normalize_audio_segment,
    validate_audio_segment,
)

APP_ROOT = Path(__file__).resolve().parent.parent
EPISODES_DIR = Path(os.getenv("EPISODES_DIR", APP_ROOT / "episodes"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", APP_ROOT / "output"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", APP_ROOT / "config"))
DATA_DIR = Path(os.getenv("DATA_DIR", APP_ROOT / "data"))
VOICE_DIR = DATA_DIR / "voices"
SAVED_EPISODES_DIR = EPISODES_DIR / "saved"
HOST_PROFILES_FILE = CONFIG_DIR / "host_profiles.json"
MAX_VOICE_UPLOAD_BYTES = int(os.getenv("MAX_VOICE_UPLOAD_MB", "50")) * 1024 * 1024
MAX_INTRO_TRACK_BYTES = int(os.getenv("MAX_INTRO_TRACK_MB", "50")) * 1024 * 1024
CHATTERBOX_URL = os.getenv("CHATTERBOX_URL", "http://chatterbox:8000").rstrip("/")
CHATTERBOX_PUBLIC_URL = os.getenv("CHATTERBOX_PUBLIC_URL", "").strip().rstrip("/")
CHATTERBOX_TTS_PATH = os.getenv("CHATTERBOX_TTS_PATH", "/v1/audio/speech")
CHATTERBOX_HEALTH_PATH = os.getenv("CHATTERBOX_HEALTH_PATH", "/health")
CHATTERBOX_VOICES_PATH = os.getenv("CHATTERBOX_VOICES_PATH", "/voices")
CHATTERBOX_PUBLIC_PORT = int(os.getenv("CHATTERBOX_PUBLIC_PORT", "8000"))
CHATTERBOX_TIMEOUT_SECONDS = float(os.getenv("CHATTERBOX_TIMEOUT_SECONDS", "600"))
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "default")
DEFAULT_TEMPO = float(os.getenv("DEFAULT_TEMPO", "1.0"))
CHATTERBOX_DEFAULTS = {
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "min_p": 0.05,
    "top_p": 1.0,
    "repetition_penalty": 1.2,
}
MAX_CHARS = int(os.getenv("MAX_CHARS_PER_CHUNK", "700"))
MIN_DUPLICATE_TRANSCRIPT_CHARS = int(os.getenv("MIN_DUPLICATE_TRANSCRIPT_CHARS", "500"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_RESPONSES_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
CONVERSATION_PROVIDER = os.getenv("CONVERSATION_PROVIDER", "openai").strip().lower()
LOCAL_AI_URL = os.getenv("LOCAL_AI_URL", "http://ollama:11434/api/chat")
LOCAL_AI_MODEL = os.getenv("LOCAL_AI_MODEL", "llama3.1:8b")
JOB_WORKERS = max(1, int(os.getenv("JOB_WORKERS", "1")))
TTS_MAX_ATTEMPTS = max(1, int(os.getenv("TTS_MAX_ATTEMPTS", "5")))
TTS_RETRY_DELAY_SECONDS = max(0.0, float(os.getenv("TTS_RETRY_DELAY_SECONDS", "15")))

EPISODES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
VOICE_DIR.mkdir(parents=True, exist_ok=True)
SAVED_EPISODES_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)

app = FastAPI(title="Podcast Builder", version="0.5.0")
app.mount("/static", StaticFiles(directory=APP_ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=APP_ROOT / "app" / "templates")

generation_queue: asyncio.Queue[str] | None = None
generation_workers: list[asyncio.Task[None]] = []


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "episode"


def clean_script(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_saved_episode(episode_json: str) -> dict[str, Any]:
    """Validate and normalize a complete, editable episode document."""
    try:
        raw = json.loads(episode_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Episode is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Episode must be an object")

    title = _text_field(raw, "title")
    if not title:
        raise HTTPException(status_code=400, detail="Episode title is required")
    hosts = parse_hosts(json.dumps(raw.get("hosts", [])))
    packet = raw.get("research_packet", {})
    if not isinstance(packet, dict):
        raise HTTPException(status_code=400, detail="Research packet must be an object")
    stories = packet.get("stories", [])
    if not isinstance(stories, list) or any(not isinstance(story, dict) for story in stories):
        raise HTTPException(status_code=400, detail="Research packet stories must be a list of objects")
    try:
        target_minutes = int(raw.get("target_minutes", 8))
        intro_music_volume = float(raw.get("intro_music_volume", 0.25))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Episode contains invalid numeric settings") from exc
    if not 2 <= target_minutes <= 60:
        raise HTTPException(status_code=400, detail="Target minutes must be between 2 and 60")
    if not 0 <= intro_music_volume <= 1:
        raise HTTPException(status_code=400, detail="Intro music volume must be between 0 and 1")
    return {
        "version": 1,
        "title": title,
        "hosts": hosts,
        "research_packet": packet,
        "story_notes": str(raw.get("story_notes", "")),
        "target_minutes": target_minutes,
        "conversation_tone": str(raw.get("conversation_tone", "")),
        "script": str(raw.get("script", "")),
        "intro_lines": str(raw.get("intro_lines", "")),
        "intro_overlap": bool(raw.get("intro_overlap", False)),
        "intro_music_volume": intro_music_volume,
    }


def _saved_episode_dir(episode_id: str) -> Path:
    safe_id = slugify(episode_id)
    if safe_id != episode_id:
        raise HTTPException(status_code=404, detail="Saved episode not found")
    return SAVED_EPISODES_DIR / safe_id


def _load_saved_episode(episode_id: str) -> tuple[Path, dict[str, Any]]:
    episode_dir = _saved_episode_dir(episode_id)
    path = episode_dir / "episode.json"
    try:
        episode = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Saved episode not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Saved episode could not be read") from exc
    return episode_dir, episode


def remove_accidental_transcript_repetition(text: str) -> str:
    """Collapse an exact, long transcript repeated two or more times in full.

    Paragraph-level matching deliberately avoids fuzzy deduplication: repeated
    catchphrases and intentional callbacks remain part of the performance.
    """
    cleaned = clean_script(text)
    paragraphs = [paragraph.strip() for paragraph in cleaned.split("\n\n")] if cleaned else []
    for unit_size in range(1, len(paragraphs) // 2 + 1):
        if len(paragraphs) % unit_size:
            continue
        unit = paragraphs[:unit_size]
        repetitions = len(paragraphs) // unit_size
        if repetitions < 2 or len("\n\n".join(unit)) < MIN_DUPLICATE_TRANSCRIPT_CHARS:
            continue
        if unit * repetitions == paragraphs:
            logger.warning(
                "Removed %s accidental full-script repetitions (%s paragraphs each)",
                repetitions - 1, unit_size,
            )
            return "\n\n".join(unit)
    return cleaned


def remove_intro_episode_overlap(intro_lines: str, script: str) -> str:
    """Remove only a long exact suffix/prefix overlap between intro and episode."""
    intro = clean_script(intro_lines)
    episode = remove_accidental_transcript_repetition(script)
    intro_paragraphs = [paragraph.strip() for paragraph in intro.split("\n\n")] if intro else []
    episode_paragraphs = [paragraph.strip() for paragraph in episode.split("\n\n")] if episode else []
    for overlap_size in range(min(len(intro_paragraphs), len(episode_paragraphs)), 0, -1):
        overlap = intro_paragraphs[-overlap_size:]
        if overlap != episode_paragraphs[:overlap_size]:
            continue
        if len("\n\n".join(overlap)) < MIN_DUPLICATE_TRANSCRIPT_CHARS:
            continue
        logger.warning(
            "Removed %s intro paragraphs duplicated at the start of the episode",
            overlap_size,
        )
        return "\n\n".join(intro_paragraphs[:-overlap_size])
    return intro


def split_script(text: str, max_chars: int = MAX_CHARS) -> List[str]:
    return [chunk["text"] for chunk in _split_script_with_boundaries(text, max_chars)]


def _split_script_with_boundaries(text: str, max_chars: int) -> list[dict[str, str | None]]:
    """Split text at natural boundaries and describe every resulting boundary."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    text = clean_script(text)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[dict[str, str | None]] = []
    current = ""
    current_boundary: str | None = None

    def flush() -> None:
        nonlocal current, current_boundary
        if current.strip():
            chunks.append({"text": current.strip(), "boundary_reason": current_boundary})
            current = ""
            current_boundary = None

    for paragraph_index, paragraph in enumerate(paragraphs):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        for sentence_index, sentence in enumerate(sentences):
            sentence = sentence.strip()
            if not sentence:
                continue
            separator = "\n\n" if sentence_index == 0 and paragraph_index > 0 else " "
            natural_boundary = "paragraph_break" if separator == "\n\n" else "sentence_break"
            candidate = f"{current}{separator}{sentence}".strip() if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
                continue

            if current:
                flush()
                current_boundary = natural_boundary

            if len(sentence) <= max_chars:
                current = sentence
                continue

            # Only sentences without a usable punctuation boundary are split by
            # words. These fragments are one utterance and receive virtually no
            # assembly pause between them.
            words = sentence.split()
            hard = ""
            for word in words:
                candidate = f"{hard} {word}".strip()
                if len(candidate) > max_chars and hard:
                    current = hard
                    flush()
                    current_boundary = "technical_continuation"
                    hard = word
                else:
                    hard = candidate
            if hard:
                # Extremely long tokens still need a final character-level
                # fallback to honor the Chatterbox input contract.
                while len(hard) > max_chars:
                    current = hard[:max_chars]
                    flush()
                    current_boundary = "technical_continuation"
                    hard = hard[max_chars:]
                current = hard

    flush()
    return chunks


def _text_field(item: dict[str, Any], key: str, default: str = "") -> str:
    return str(item.get(key, default) or "").strip()


def parse_hosts(hosts_json: str) -> list[dict[str, Any]]:
    try:
        raw_hosts = json.loads(hosts_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Host configuration is invalid JSON") from exc

    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise HTTPException(status_code=400, detail="At least one host is required")

    hosts: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_ids: set[str] = set()
    allowed_interruptions = {"low", "medium", "high"}
    for index, item in enumerate(raw_hosts, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"Host {index} is invalid")

        name = _text_field(item, "name")
        host_id = _text_field(item, "id") or uuid4().hex
        if not re.fullmatch(r"[a-fA-F0-9]{32}", host_id):
            raise HTTPException(status_code=400, detail=f"Host {name or index} has an invalid ID")
        if host_id.lower() in seen_ids:
            raise HTTPException(status_code=400, detail="Host IDs must be unique")
        voice = _text_field(item, "voice")
        try:
            tempo = float(item.get("tempo", DEFAULT_TEMPO))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Host {index} has an invalid tempo") from exc

        if not name:
            raise HTTPException(status_code=400, detail=f"Host {index} needs a name")
        if name.casefold() in seen:
            raise HTTPException(status_code=400, detail=f"Host names must be unique: {name}")
        if not voice:
            raise HTTPException(status_code=400, detail=f"Host {name} needs a Chatterbox voice")
        if tempo < 0.5 or tempo > 2.0:
            raise HTTPException(status_code=400, detail=f"Tempo for {name} must be between 0.5 and 2.0")

        chatterbox: dict[str, float] = {}
        ranges = {
            "exaggeration": (0.0, 2.0),
            "cfg_weight": (0.0, 1.0),
            "temperature": (0.05, 5.0),
            "min_p": (0.0, 1.0),
            "top_p": (0.0, 1.0),
            "repetition_penalty": (0.0, 2.0),
        }
        for field, default in CHATTERBOX_DEFAULTS.items():
            try:
                value = float(item.get(field, default))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{field} for {name} must be a number") from exc
            minimum, maximum = ranges[field]
            if value < minimum or value > maximum:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field} for {name} must be between {minimum:g} and {maximum:g}",
                )
            chatterbox[field] = value

        interruption_frequency = _text_field(item, "interruption_frequency", "medium").lower()
        if interruption_frequency not in allowed_interruptions:
            raise HTTPException(
                status_code=400,
                detail=f"Interruption frequency for {name} must be low, medium, or high",
            )

        raw_traits = item.get("traits", [])
        if isinstance(raw_traits, str):
            traits = [part.strip() for part in raw_traits.split(",") if part.strip()]
        elif isinstance(raw_traits, list):
            traits = [str(part).strip() for part in raw_traits if str(part).strip()]
        else:
            raise HTTPException(status_code=400, detail=f"Traits for {name} must be a list or comma-separated text")

        seen.add(name.casefold())
        seen_ids.add(host_id.lower())
        hosts.append(
            {
                "id": host_id.lower(),
                "name": name,
                "role": _text_field(item, "role"),
                "voice": voice,
                "tempo": tempo,
                **chatterbox,
                "traits": traits,
                "debate_style": _text_field(item, "debate_style"),
                "humor_style": _text_field(item, "humor_style"),
                "interruption_frequency": interruption_frequency,
                "sentence_style": _text_field(item, "sentence_style"),
                "political_posture": _text_field(item, "political_posture"),
                "flaws": _text_field(item, "flaws"),
                "character_notes": _text_field(item, "character_notes"),
                "reference_audio_path": (
                    f"voices/{host_id.lower()}/reference.wav"
                    if _text_field(item, "reference_audio_path") else None
                ),
                "reference_audio_filename": _text_field(item, "reference_audio_filename") or None,
                "reference_audio_available": bool(
                    _text_field(item, "reference_audio_path")
                    and (VOICE_DIR / host_id.lower() / "reference.wav").is_file()
                ),
            }
        )

    return hosts


def load_host_profiles() -> list[dict[str, Any]]:
    if not HOST_PROFILES_FILE.exists():
        return [{"name": "Host", "voice": DEFAULT_VOICE, "tempo": DEFAULT_TEMPO}]
    try:
        return parse_hosts(HOST_PROFILES_FILE.read_text(encoding="utf-8"))
    except (HTTPException, OSError):
        return [{"name": "Host", "voice": DEFAULT_VOICE, "tempo": DEFAULT_TEMPO}]


def save_host_profiles(hosts: list[dict[str, Any]]) -> None:
    HOST_PROFILES_FILE.write_text(json.dumps(hosts, indent=2) + "\n", encoding="utf-8")


def find_host(host_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not re.fullmatch(r"[a-fA-F0-9]{32}", host_id):
        raise HTTPException(status_code=404, detail="Host not found")
    hosts = load_host_profiles()
    host = next((item for item in hosts if item["id"] == host_id.lower()), None)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return hosts, host


def reference_path(host: dict[str, Any]) -> Path:
    return VOICE_DIR / host["id"] / "reference.wav"


def voice_metadata(host: dict[str, Any]) -> dict[str, Any]:
    configured = bool(host.get("reference_audio_path"))
    available = configured and reference_path(host).is_file()
    return {
        "host_id": host["id"],
        "reference_voice": {
            "configured": configured,
            "available": available,
            "filename": host.get("reference_audio_filename") or ("reference.wav" if configured else None),
            "playback_url": f"/api/hosts/{host['id']}/voice/audio" if available else None,
        },
        "exaggeration": host["exaggeration"],
        "cfg_weight": host["cfg_weight"],
    }


def save_host_update(hosts: list[dict[str, Any]], host: dict[str, Any]) -> None:
    save_host_profiles([host if item["id"] == host["id"] else item for item in hosts])


def normalize_reference_audio(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".wav.tmp")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Audio conversion is temporarily unavailable.") from exc
    if result.returncode or not temporary.is_file() or not _valid_wav(temporary):
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The selected file could not be decoded as audio.")
    temporary.replace(destination)


def normalize_intro_track(source: Path, destination: Path) -> None:
    """Convert an uploaded intro into the same WAV format used by Chatterbox."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".wav.tmp")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Intro track conversion is temporarily unavailable.") from exc
    if result.returncode or not _valid_wav(temporary):
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The intro track could not be decoded as audio.")
    temporary.replace(destination)


def mix_intro_track_and_voice(
    track: Path, voice: Path, destination: Path, music_volume: float = 0.25
) -> None:
    """Mix intro speech over a ducked music track, keeping the longer input."""
    if not 0.0 <= music_volume <= 1.0:
        raise ValueError("intro music volume must be between 0 and 1")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".wav.tmp")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(track), "-i", str(voice),
        "-filter_complex",
        f"[0:a]volume={music_volume:.3f}[music];"
        "[music][1:a]amix=inputs=2:duration=longest:dropout_transition=0,alimiter=limit=0.95[mixed]",
        "-map", "[mixed]", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
        "-f", "wav", str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is not installed in the application container") from exc
    if result.returncode or not _valid_wav(temporary):
        temporary.unlink(missing_ok=True)
        detail = result.stderr.decode(errors="replace").strip() if result.stderr else ""
        raise RuntimeError(f"Could not mix the intro music and voice{': ' + detail if detail else ''}")
    temporary.replace(destination)


def parse_speaker_script(script: str, hosts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Split a script into ordered speaker sections.

    A line containing only ``[Host Name]`` switches the active speaker. Names are
    case-sensitive, and text before the first speaker tag uses the first configured
    host.
    """
    cleaned = clean_script(script)
    if not cleaned:
        return []

    canonical_names = {host["name"] for host in hosts}
    active_host = hosts[0]["name"]
    buffer: list[str] = []
    sections: list[dict[str, str]] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            sections.append({"host": active_host, "text": text})
        buffer = []

    for line in cleaned.splitlines():
        match = re.fullmatch(r"\s*\[([^\]\n]+)\]\s*", line)
        if match:
            requested = match.group(1).strip()
            if requested not in canonical_names:
                available = ", ".join(host["name"] for host in hosts)
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown host tag [{requested}]. Configured hosts: {available}",
                )
            if requested == active_host:
                logger.info("Ignoring redundant speaker tag [%s]", requested)
                continue
            flush()
            active_host = requested
            continue

        # A speaker marker must occupy its own line. Without this check an
        # inline marker would be passed through to TTS and spoken aloud.
        inline_tag = re.match(r"\s*\[([^\]\n]+)\]", line)
        if inline_tag and inline_tag.group(1).strip() in canonical_names:
            raise HTTPException(
                status_code=400,
                detail="Speaker tags must be on their own line, with dialogue on the following line",
            )
        buffer.append(line)

    flush()
    return sections


def build_speech_chunks(
    script: str,
    hosts: list[dict[str, Any]],
    max_chars: int = MAX_CHARS,
    section_id: str = "episode",
) -> list[dict[str, Any]]:
    """Build the ordered utterance manifest used by synthesis and editing.

    IDs are opaque manifest identities: unlike sequence numbers and filenames they
    do not change when an utterance is moved.  A long turn may produce multiple
    utterances, all linked by one ``parent_turn_id``.  Each utterance is exactly
    one Chatterbox request and can therefore never contain multiple speakers.
    """
    host_lookup = {host["name"]: host for host in hosts}
    sections = parse_speaker_script(remove_accidental_transcript_repetition(script), hosts)
    chunks: list[dict[str, Any]] = []

    for turn_number, section in enumerate(sections, start=1):
        host = host_lookup[section["host"]]
        parent_turn_id = uuid4().hex
        voice = host["voice"]
        if host.get("reference_audio_path"):
            path = reference_path(host)
            if path.is_file():
                voice = f"host-{host['id']}"
            else:
                logger.warning(
                    "Reference voice missing for host %s (%s). Falling back to default voice.",
                    host["name"], host["id"],
                )
                voice = DEFAULT_VOICE
        fragments = _split_script_with_boundaries(section["text"], max_chars=max_chars)
        fragments = [fragment for fragment in fragments if is_speakable_text(str(fragment["text"]))]
        for fragment_index, fragment in enumerate(fragments, start=1):
            text_chunk = str(fragment["text"])
            if not is_speakable_text(text_chunk):
                logger.warning("Skipping non-speaking chunk for %s: %r", host["name"], text_chunk)
                continue
            utterance_id = uuid4().hex
            settings = {
                "tempo": host["tempo"],
                **{field: host.get(field, default) for field, default in CHATTERBOX_DEFAULTS.items()},
            }
            chunks.append(
                {
                    "id": utterance_id,
                    "sequence": len(chunks) + 1,
                    "section_id": section_id,
                    "parent_turn_id": parent_turn_id,
                    "fragment_index": fragment_index,
                    "fragment_count": len(fragments),
                    "host_id": host.get("id"),
                    "display_name": host["name"],
                    "normalized_text": text_chunk,
                    "voice_revision": {
                        "voice": voice,
                        "reference": host.get("reference_audio_path"),
                    },
                    "synthesis_settings": settings,
                    "raw_output": None,
                    "normalized_output": None,
                    "timing": {},
                    "transition": {"dramatic_pause_after": False},
                    # Compatibility fields for the renderer and older manifests.
                    "host": host["name"],
                    "voice": voice,
                    **settings,
                    "text": text_chunk,
                    "boundary_reason": (
                        "speaker_change" if turn_number > 1 and fragment_index == 1
                        else fragment["boundary_reason"]
                    ),
                }
            )
    return chunks


def build_episode_speech_chunks(
    script: str,
    hosts: list[dict[str, Any]],
    intro_lines: str = "",
    max_chars: int = MAX_CHARS,
) -> list[dict[str, Any]]:
    """Build optional host introductions followed by the main episode script."""
    canonical_script = remove_accidental_transcript_repetition(script)
    canonical_intro = remove_intro_episode_overlap(intro_lines, canonical_script)
    intro_chunks = build_speech_chunks(canonical_intro, hosts, max_chars, "host_intro") if canonical_intro else []
    episode_chunks = build_speech_chunks(canonical_script, hosts, max_chars, "episode")
    utterances = [*intro_chunks, *episode_chunks]
    for sequence, utterance in enumerate(utterances, start=1):
        utterance["sequence"] = sequence
        utterance["section"] = utterance["section_id"]
    return utterances


def build_conversation_prompt(
    story_notes: str,
    hosts: list[dict[str, Any]],
    target_minutes: int = 8,
    tone: str = "smart, funny, conversational, politically non-tribal",
) -> str:
    if not story_notes.strip():
        raise HTTPException(status_code=400, detail="Story/source notes cannot be empty")
    if target_minutes < 2 or target_minutes > 60:
        raise HTTPException(status_code=400, detail="Target length must be between 2 and 60 minutes")

    cast = []
    for host in hosts:
        cast.append(
            f"""HOST: {host['name']}
Role: {host.get('role', '')}
Traits: {', '.join(host.get('traits', []))}
Debate style: {host.get('debate_style', '')}
Humor style: {host.get('humor_style', '')}
Interruption tendency: {host.get('interruption_frequency', 'medium')}
Speaking style: {host.get('sentence_style', '')}
Political posture: {host.get('political_posture', '')}
Flaws: {host.get('flaws', '')}
Character notes: {host.get('character_notes', '')}"""
        )

    names = ", ".join(host["name"] for host in hosts)
    first_speaker = hosts[0]["name"]
    switch_example = (
        f"- Format every speaker change as the host's exact configured name in square brackets on its own line, "
        f"followed by dialogue on the next line, for example:\n[{hosts[1]['name']}]\nSpoken words here."
        if len(hosts) > 1
        else "- There is only one host, so do not emit any speaker tags."
    )
    return f"""You are writing a podcast conversation for the show described by the supplied cast, tone, and source notes.

GOAL
Create a natural, entertaining discussion among {names}. Capture the ingredients of excellent podcast chemistry without imitating any real host, show, comedian, or public figure. The hosts like one another. They can disagree, tease, interrupt, correct, and challenge each other without becoming partisan caricatures.

TONE
{tone}

TARGET LENGTH
About {target_minutes} spoken minutes. Favor tight back-and-forth over long monologues.

CAST

{chr(10).join(cast)}

SOURCE / STORY NOTES
{story_notes.strip()}

WRITING RULES
- Stay grounded in the supplied notes. Do not invent factual details, quotes, polling numbers, dates, or allegations.
- If the notes leave something uncertain, have a host explicitly say it is uncertain or needs verification.
- No host is permanently correct. Let different hosts make the strongest point at different moments.
- Keep every host nuanced: practical reasoning should not become anti-intellectual, analysis should not become smug, and context should not become a lecture.
- Use callbacks, follow-up questions, occasional interruptions, short reactions, and friendly roasting.
- Avoid repetitive agreement phrases and obvious turn-taking.
- Let a host occasionally change their mind or concede a point.
- Do not describe actions, sound effects, emotions, or stage directions in brackets. Brackets are reserved only for exact speaker tags.
- Output narration only. No Markdown headings, preamble, commentary, or explanation.
- {first_speaker} is the first speaker. Begin directly with {first_speaker}'s dialogue; text before the first speaker tag belongs to {first_speaker}.
- Insert a speaker tag only when the active speaker changes. Consecutive paragraphs from the same speaker do not need another tag.
{switch_example}
- Never put dialogue on the same line as a speaker tag, and never add a colon after a speaker name.
- Use only these exact speaker names: {names}. Never emit role labels or invented speaker names.
- Make the first 20 seconds hook the listener. End with a clean transition or takeaway rather than a generic summary.
"""


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts).strip()


def parse_model_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    """Decode a model response while preserving a useful upstream error."""
    try:
        payload = response.json()
    except ValueError as exc:
        detail = response.text.strip().replace("\n", " ")[:500]
        message = f"{provider} returned an invalid JSON response"
        if detail:
            message = f"{message}: {detail}"
        raise HTTPException(status_code=502, detail=message) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"{provider} returned an invalid JSON response")
    return payload


async def generate_conversation_with_openai(prompt: str) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Conversation generation is configured, but OPENAI_API_KEY is not set. Add it to your .env or Docker environment.",
        )
    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(OPENAI_RESPONSES_URL, headers=headers, json=payload)
            response.raise_for_status()
            text = extract_response_text(parse_model_json(response, "Conversation model"))
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise HTTPException(status_code=502, detail=f"Conversation model request failed: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Conversation model request failed: {exc}") from exc
    if not text:
        raise HTTPException(status_code=502, detail="Conversation model returned no text")
    return text


async def generate_conversation_locally(prompt: str) -> str:
    """Generate a draft with Ollama's native chat API.

    Keeping this adapter separate from prompt construction makes it possible to
    replace Ollama with another local runtime without touching the conversation
    or media pipelines.
    """
    payload = {
        "model": LOCAL_AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(LOCAL_AI_URL, json=payload)
            response.raise_for_status()
            body = parse_model_json(response, "Local conversation model")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Local conversation model returned HTTP {exc.response.status_code}: {exc.response.text[:500]}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach local conversation model: {exc}") from exc
    text = str(body.get("message", {}).get("content", "")).strip()
    if not text:
        raise HTTPException(status_code=502, detail="Local conversation model returned no text")
    return text


async def generate_conversation(prompt: str) -> str:
    if CONVERSATION_PROVIDER == "local":
        return await generate_conversation_locally(prompt)
    if CONVERSATION_PROVIDER == "openai":
        return await generate_conversation_with_openai(prompt)
    raise HTTPException(
        status_code=503,
        detail="CONVERSATION_PROVIDER must be 'openai' or 'local'",
    )


def conversation_model_name() -> str:
    return LOCAL_AI_MODEL if CONVERSATION_PROVIDER == "local" else OPENAI_MODEL


def chatterbox_endpoint(path: str) -> str:
    """Join a configurable Chatterbox API path to its internal service URL."""
    return f"{CHATTERBOX_URL}/{path.lstrip('/')}"


async def chatterbox_status() -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
        response = await client.get(chatterbox_endpoint(CHATTERBOX_HEALTH_PATH))
        response.raise_for_status()
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return {"detail": response.text[:200] or "Chatterbox is reachable"}


async def chatterbox_voices() -> list[dict[str, Any]]:
    """Fetch the voices that the bundled Chatterbox service can actually use."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(chatterbox_endpoint(CHATTERBOX_VOICES_PATH))
        response.raise_for_status()
    payload = response.json()
    voices = payload.get("voices", [])
    if not isinstance(voices, list):
        raise ValueError("Chatterbox returned an invalid voice list")
    return voices


def describe_chatterbox_error(exc: httpx.HTTPError) -> str:
    """Return an actionable message even when httpx's exception text is empty."""
    endpoint = chatterbox_endpoint(CHATTERBOX_TTS_PATH)
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        response_detail = response.text.strip().replace("\n", " ")[:500]
        message = f"Chatterbox returned HTTP {response.status_code} from {endpoint}"
        return f"{message}: {response_detail}" if response_detail else message
    if isinstance(exc, httpx.TimeoutException):
        return f"Chatterbox did not respond within {CHATTERBOX_TIMEOUT_SECONDS:g} seconds at {endpoint}"

    reason = str(exc).strip()
    if reason:
        return f"Could not reach Chatterbox at {endpoint}: {reason}"
    return f"Could not reach Chatterbox at {endpoint} ({type(exc).__name__})"


async def synthesize_chunk(
    text: str, voice: str, tempo: float, destination: Path, **chatterbox_settings: float
) -> None:
    """Generate WAV audio through the local Chatterbox service."""
    payload = {
        "input": text,
        "model": "chatterbox",
        "voice": voice,
        "speed": tempo,
        "response_format": "wav",
        **{field: chatterbox_settings.get(field, default) for field, default in CHATTERBOX_DEFAULTS.items()},
    }
    timeout = httpx.Timeout(CHATTERBOX_TIMEOUT_SECONDS, connect=10, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(chatterbox_endpoint(CHATTERBOX_TTS_PATH), json=payload)
        response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    if not response.content.startswith(b"RIFF"):
        detail = response.text.strip().replace("\n", " ")[:500] if "json" in content_type else ""
        suffix = f": {detail}" if detail else f" (content type: {content_type or 'unknown'})"
        raise httpx.HTTPStatusError(
            f"Chatterbox returned a response that is not WAV audio{suffix}",
            request=response.request,
            response=response,
        )
    destination.write_bytes(response.content)


def assemble_mp3(
    chunk_paths: List[Path], output_file: Path, chunks: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Build a zero-based WAV timeline, then encode it; pauses are added only here."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed in the application container")

    chunks = chunks or [{"host": "unknown", "text": ""} for _ in chunk_paths]
    timeline_wav = output_file.with_suffix(".timeline.wav")
    timeline = concatenate_wav_segments(chunk_paths, chunks, timeline_wav)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        timeline_wav.name,
        "-af",
        "asetpts=PTS-STARTPTS",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        output_file.name,
    ]
    try:
        subprocess.run(cmd, cwd=output_file.parent, check=True)
    finally:
        timeline_wav.unlink(missing_ok=True)
    return timeline


def build_generation_segments(speech_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the ordered, durable speech chunks stored by a generation job.

    The historical name is retained for callers, but this function no longer
    creates arbitrary fixed-size segments. Each speech chunk is independently
    resumable and is the unit from which the final episode is assembled.
    """
    return [
        {
            **chunk,
            "id": f"chunk-{number:03d}",
            "number": number,
            "status": "queued",
            "attempt": 0,
            "output": None,
            "normalized_output": None,
            "audio_metrics": None,
            "error": None,
        }
        for number, chunk in enumerate(speech_chunks, start=1)
    ]


def _migrate_job_manifest(job: dict[str, Any]) -> bool:
    """Read legacy segment manifests as an ordered top-level chunk manifest."""
    if isinstance(job.get("chunks"), list):
        return False
    segments = job.pop("segments", None)
    if not isinstance(segments, list):
        job["chunks"] = []
        return True
    chunks = [chunk for segment in segments for chunk in segment.get("chunks", [])]
    for number, chunk in enumerate(chunks, start=1):
        chunk.setdefault("id", f"chunk-{number:03d}")
        chunk.setdefault("number", number)
        chunk.setdefault("status", "queued")
        chunk.setdefault("attempt", 0)
        chunk.setdefault("output", None)
        chunk.setdefault("normalized_output", None)
        chunk.setdefault("audio_metrics", None)
        chunk.setdefault("error", None)
    job["chunks"] = chunks
    return True


def _write_job(job_dir: Path, job: dict[str, Any]) -> None:
    temporary = job_dir / "job.json.tmp"
    temporary.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    temporary.replace(job_dir / "job.json")


def _load_job(job_id: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", job_id):
        raise HTTPException(status_code=400, detail="Invalid generation job id")
    job_dir = OUTPUT_DIR / job_id
    job_file = job_dir / "job.json"
    if not job_file.exists():
        raise HTTPException(status_code=404, detail="Generation job not found")
    job = json.loads(job_file.read_text(encoding="utf-8"))
    if _migrate_job_manifest(job):
        _write_job(job_dir, job)
    return job_dir, job


def _job_progress(job: dict[str, Any]) -> dict[str, int]:
    _migrate_job_manifest(job)
    chunks = job["chunks"]
    return {
        "complete": sum(chunk.get("status") == "complete" for chunk in chunks),
        "total": len(chunks),
    }


async def generation_worker(worker_number: int) -> None:
    """Consume durable jobs serially by default, which is safer on CPU-only hosts."""
    assert generation_queue is not None
    while True:
        job_id = await generation_queue.get()
        try:
            await process_generation_job(job_id)
        finally:
            generation_queue.task_done()


def _valid_wav(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 44 and path.read_bytes()[:4] == b"RIFF"
    except OSError:
        return False


def normalized_chunk_path(path: Path) -> Path:
    """Keep processed audio separate so a good Chatterbox render is never overwritten."""
    return path.parent / "normalized" / path.name


def _retryable_tts_error(exc: httpx.HTTPError) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return True
    return exc.response.status_code in {408, 425, 429} or exc.response.status_code >= 500


async def synthesize_chunk_with_retry(
    job_dir: Path,
    job: dict[str, Any],
    chunk: dict[str, Any],
    destination: Path,
) -> None:
    """Retry backend crashes/disconnects and checkpoint every attempt."""
    temporary = destination.with_suffix(".wav.tmp")
    for attempt in range(1, TTS_MAX_ATTEMPTS + 1):
        chunk["status"] = "running"
        chunk["attempt"] = attempt
        chunk["error"] = None
        _write_job(job_dir, job)
        try:
            temporary.unlink(missing_ok=True)
            await synthesize_chunk(
                chunk["text"], chunk["voice"], chunk["tempo"], temporary,
                **{field: chunk.get(field, default) for field, default in CHATTERBOX_DEFAULTS.items()},
            )
            temporary.replace(destination)
            normalized = normalized_chunk_path(destination)
            metrics = normalize_audio_segment(destination, normalized)
            valid, reason = validate_audio_segment(normalized)
            if not valid:
                normalized.unlink(missing_ok=True)
                raise OSError(reason)
            chunk["audio_metrics"] = metrics
            chunk["normalized_output"] = normalized.relative_to(job_dir).as_posix()
            chunk["status"] = "complete"
            chunk["output"] = destination.relative_to(job_dir).as_posix()
            chunk["raw_output"] = chunk["output"]
            _write_job(job_dir, job)
            return
        except (httpx.HTTPError, OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            chunk["error"] = describe_chatterbox_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
            can_retry = attempt < TTS_MAX_ATTEMPTS and (
                not isinstance(exc, httpx.HTTPError) or _retryable_tts_error(exc)
            )
            chunk["status"] = "retrying" if can_retry else "failed"
            _write_job(job_dir, job)
            if not can_retry:
                raise
            await asyncio.sleep(TTS_RETRY_DELAY_SECONDS * attempt)


async def queue_generation_job(job_id: str) -> None:
    if generation_queue is None:
        raise HTTPException(status_code=503, detail="The audio job worker is not ready")
    await generation_queue.put(job_id)


async def start_generation_workers() -> None:
    """Start workers and recover work interrupted by a container restart."""
    global generation_queue
    generation_queue = asyncio.Queue()
    for number in range(JOB_WORKERS):
        generation_workers.append(asyncio.create_task(generation_worker(number + 1)))

    for manifest in sorted(OUTPUT_DIR.glob("*/job.json")):
        try:
            job = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _migrate_job_manifest(job)
        if job.get("status") not in {"queued", "running"}:
            continue
        # Keep valid completed chunks: CPU renders may represent hours of work.
        job["status"] = "queued"
        job["error"] = None
        job.pop("started_at", None)
        job.pop("finished_at", None)
        for chunk in job["chunks"]:
            output = chunk.get("output")
            if chunk.get("status") == "complete" and output and _valid_wav(manifest.parent / output):
                continue
            chunk["status"] = "queued"
            chunk["output"] = None
            chunk["normalized_output"] = None
            chunk["audio_metrics"] = None
            chunk["error"] = None
        _write_job(manifest.parent, job)
        await generation_queue.put(job["id"])


async def stop_generation_workers() -> None:
    global generation_queue
    for worker in generation_workers:
        worker.cancel()
    if generation_workers:
        await asyncio.gather(*generation_workers, return_exceptions=True)
    generation_workers.clear()
    generation_queue = None


@asynccontextmanager
async def application_lifespan(_app: FastAPI):
    await start_generation_workers()
    try:
        yield
    finally:
        await stop_generation_workers()


app.router.lifespan_context = application_lifespan


async def process_generation_job(job_id: str) -> None:
    """Render durable speech chunks and assemble the final episode from them."""
    job_dir, job = _load_job(job_id)
    job["status"] = "running"
    job["started_at"] = datetime.now(timezone.utc).isoformat()
    job["worker_pid"] = os.getpid()
    _write_job(job_dir, job)
    all_chunk_paths: list[Path] = []
    all_chunks: list[dict[str, Any]] = []
    intro_track = job.get("intro", {}).get("track")
    if intro_track:
        intro_path = job_dir / intro_track
        if not _valid_wav(intro_path):
            job["status"] = "failed"
            job["error"] = "The saved intro track is missing or invalid"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_job(job_dir, job)
            return
        all_chunk_paths.append(intro_path)
        all_chunks.append({"host": "intro", "text": "", "section": "intro", "dramatic_pause_after": True})
    try:
        for chunk in job["chunks"]:
            path = job_dir / "chunks" / f"utterance-{chunk['id']}.wav"
            saved_output = chunk.get("output")
            saved_path = job_dir / saved_output if saved_output else path
            if chunk.get("status") == "complete" and _valid_wav(saved_path):
                raw_path = saved_path
                saved_normalized = chunk.get("normalized_output")
                path = job_dir / saved_normalized if saved_normalized else normalized_chunk_path(raw_path)
                if not _valid_wav(path) or not chunk.get("audio_metrics"):
                    chunk["audio_metrics"] = normalize_audio_segment(raw_path, path)
                    chunk["normalized_output"] = path.relative_to(job_dir).as_posix()
                valid, reason = validate_audio_segment(path)
                if not valid:
                    raise OSError(reason)
            else:
                await synthesize_chunk_with_retry(job_dir, job, chunk, path)
                path = job_dir / chunk["normalized_output"]
            all_chunk_paths.append(path)
            all_chunks.append(chunk)
            _write_job(job_dir, job)

        final_paths = all_chunk_paths
        final_chunks = all_chunks
        intro = job.get("intro", {})
        chunks_after_track = all_chunks[1:] if intro_track else all_chunks
        intro_chunk_count = next(
            (index for index, chunk in enumerate(chunks_after_track) if chunk.get("section") != "host_intro"),
            len(chunks_after_track),
        )
        if intro.get("overlap") and intro_track and intro_chunk_count:
            intro_voice_wav = job_dir / "intro-voice.wav"
            mixed_intro_wav = job_dir / "intro-mixed.wav"
            concatenate_wav_segments(
                all_chunk_paths[1:1 + intro_chunk_count],
                all_chunks[1:1 + intro_chunk_count],
                intro_voice_wav,
            )
            mix_intro_track_and_voice(
                job_dir / intro_track,
                intro_voice_wav,
                mixed_intro_wav,
                float(intro.get("music_volume", 0.25)),
            )
            intro_voice_wav.unlink(missing_ok=True)
            final_paths = [mixed_intro_wav, *all_chunk_paths[1 + intro_chunk_count:]]
            final_chunks = [
                {"host": "intro", "text": intro.get("lines", ""), "section": "intro", "boundary_reason": None},
                *all_chunks[1 + intro_chunk_count:],
            ]
            if len(final_chunks) > 1:
                final_chunks[1] = {**final_chunks[1], "boundary_reason": "explicit_dramatic_pause"}

        final_path = job_dir / f"{slugify(job['title'])}.mp3"
        timeline_wav = job_dir / "final-audio-qa.wav"
        timeline = concatenate_wav_segments(final_paths, final_chunks, timeline_wav)
        for item, chunk in zip(timeline[-len(final_chunks):], final_chunks):
            if chunk.get("id"):
                chunk["timing"] = {
                    key: round(float(item[key]), 3) for key in ("duration", "start", "end")
                }
                chunk.setdefault("transition", {})["pause_after_ms"] = item["pause_after_ms"]
            metrics = chunk.get("audio_metrics", {})
            logger.info(
                "Chunk %s | Speaker: %s | Text length: %s | Raw duration: %.3fs | "
                "Leading silence: %.3fs | Trailing silence: %.3fs | Configured pause: %.3fs | "
                "Final chunk duration: %.3fs | Effective gap before next speech: %.3fs | "
                "Timeline: %.3f-%.3fs",
                item["index"], item["speaker"], item["text_length"],
                float(metrics.get("raw_duration", item["duration"])),
                float(metrics.get("raw_leading_silence", 0)),
                float(metrics.get("raw_trailing_silence", 0)), item["pause_after_ms"] / 1000,
                item["duration"], float(metrics.get("trailing_silence", 0)) + item["pause_after_ms"] / 1000,
                item["start"], item["end"],
            )
        qa = analyze_final_audio_silence(timeline_wav)
        for region in qa:
            logger.warning("Audio QA: %.3f - %.3f | Silence: %.3f seconds",
                           region["start"], region["end"], region["duration"])
        assemble_mp3(final_paths, final_path, final_chunks)
        job["audio_qa"] = qa
        job["timeline"] = timeline
        timeline_wav.unlink(missing_ok=True)
        job["status"] = "complete"
        job["download_url"] = f"/api/episodes/{job_id}/download"
    except Exception as exc:  # Persist failures so polling clients never hang.
        job["status"] = "failed"
        job["error"] = describe_chatterbox_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        for chunk in job["chunks"]:
            if chunk["status"] == "running":
                chunk["status"] = "failed"
                chunk["error"] = job["error"]
                break
    finally:
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_job(job_dir, job)


def media_duration(path: Path) -> float:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed in the application container")
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return round(float(result.stdout.strip()), 3)


def enrich_chunk_timeline(metadata: dict[str, Any], episode_dir: Path) -> dict[str, Any]:
    """Measure ordered utterances without deriving identity from file ordering."""
    cursor = 0.0
    utterances = metadata.get("utterances", metadata.get("chunks", []))
    for item in sorted(utterances, key=lambda value: value.get("sequence", 0)):
        output = item.get("normalized_output") or item.get("raw_output") or item.get("output")
        if not output:
            return metadata
        chunk_file = episode_dir / output
        if not chunk_file.is_file():
            return metadata
        duration = media_duration(chunk_file)
        timing = item.setdefault("timing", {})
        timing["duration"] = duration
        timing["start"] = round(cursor, 3)
        cursor += duration
        timing["end"] = round(cursor, 3)
        # Temporary aliases keep existing clients readable during migration.
        item.update(timing)
    metadata["duration"] = round(cursor, 3)
    return metadata


def ordered_utterances(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return manifest utterances in editorial order (with legacy fallback)."""
    return sorted(
        metadata.get("utterances", metadata.get("chunks", [])),
        key=lambda item: item.get("sequence", item.get("number", 0)),
    )


def _clip_score(text: str, speakers: int, duration: float) -> float:
    score = 0.0
    lower = text.lower()
    score += min(len(text) / 260.0, 3.0)
    score += min(speakers - 1, 3) * 1.35
    score += text.count("?") * 0.5 + text.count("!") * 0.35
    for token in ("but ", "here's", "here is", "problem", "actually", "because", "why ", "wait", "hold on", "the thing is", "that's the point", "that is the point"):
        if token in lower:
            score += 0.35
    if 25 <= duration <= 50:
        score += 1.0
    elif 18 <= duration <= 60:
        score += 0.5
    return round(score, 3)


def suggest_clip_windows(metadata: dict[str, Any], min_seconds: float = 20, max_seconds: float = 60, limit: int = 5) -> list[dict[str, Any]]:
    chunks = ordered_utterances(metadata)
    if not chunks or any("start" not in c.get("timing", c) or "end" not in c.get("timing", c) for c in chunks):
        return []
    candidates: list[dict[str, Any]] = []
    for start_idx in range(len(chunks)):
        speakers: set[str] = set()
        texts: list[str] = []
        start = float(chunks[start_idx].get("timing", chunks[start_idx])["start"])
        for end_idx in range(start_idx, len(chunks)):
            c = chunks[end_idx]
            end = float(c.get("timing", c)["end"])
            duration = end - start
            if duration > max_seconds:
                break
            speakers.add(str(c.get("display_name", c.get("host", "Host"))))
            texts.append(str(c.get("normalized_text", c.get("text", ""))))
            if duration >= min_seconds:
                text = " ".join(texts)
                candidates.append({
                    "start": round(start, 2), "end": round(end, 2), "duration": round(duration, 2),
                    "speakers": sorted(speakers), "preview": text[:260].strip(),
                    "score": _clip_score(text, len(speakers), duration),
                })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        # Avoid returning five heavily overlapping versions of the same moment.
        overlap = False
        for chosen in selected:
            intersection = max(0.0, min(candidate["end"], chosen["end"]) - max(candidate["start"], chosen["start"]))
            if intersection / min(candidate["duration"], chosen["duration"]) > 0.55:
                overlap = True
                break
        if not overlap:
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _ass_escape(text: str) -> str:
    text = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return text.replace("\n", r"\N")


def build_clip_ass(metadata: dict[str, Any], start: float, end: float, destination: Path, width: int, height: int) -> None:
    font_size = 64 if height >= 1600 else 42
    margin_v = int(height * 0.18)
    header = f"""[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&HCC000000,&H88000000,-1,0,0,0,100,100,0,0,1,4,1,2,70,70,{margin_v},1\nStyle: Speaker,DejaVu Sans,{max(30, int(font_size*.52))},&H0058A6FF,&H000000FF,&HCC000000,&H88000000,-1,0,0,0,100,100,0,0,1,3,0,2,70,70,{max(60, int(margin_v*.62))},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    events: list[str] = []
    for chunk in ordered_utterances(metadata):
        timing = chunk.get("timing", chunk)
        c_start = float(timing.get("start", 0))
        c_end = float(timing.get("end", 0))
        if c_end <= start or c_start >= end:
            continue
        rel_start = max(c_start, start) - start
        rel_end = min(c_end, end) - start
        host = _ass_escape(str(chunk.get("display_name", chunk.get("host", ""))))
        text = _ass_escape(str(chunk.get("normalized_text", chunk.get("text", ""))))
        # Split very long TTS chunks into visual sentences while keeping timing proportional.
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()] or [text]
        total_chars = max(sum(len(x) for x in sentences), 1)
        cursor = rel_start
        for sentence in sentences:
            span = (rel_end - rel_start) * (len(sentence) / total_chars)
            next_cursor = min(rel_end, cursor + max(span, 0.6))
            events.append(f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(next_cursor)},Default,,0,0,0,,{sentence}")
            events.append(f"Dialogue: 1,{_ass_time(cursor)},{_ass_time(next_cursor)},Speaker,,0,0,0,,{host}")
            cursor = next_cursor
    destination.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def render_social_clip(episode_dir: Path, start: float, end: float, title: str, aspect: str, destination: Path, metadata: dict[str, Any]) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed in the application container")
    presets = {"vertical": (1080, 1920), "square": (1080, 1080), "horizontal": (1920, 1080)}
    if aspect not in presets:
        raise HTTPException(status_code=400, detail="Aspect must be vertical, square, or horizontal")
    width, height = presets[aspect]
    duration = end - start
    if duration < 5 or duration > 90:
        raise HTTPException(status_code=400, detail="Clip length must be between 5 and 90 seconds")
    mp3_files = list(episode_dir.glob("*.mp3"))
    if not mp3_files:
        raise HTTPException(status_code=404, detail="Episode audio not found")
    clips_dir = episode_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    ass_file = clips_dir / f"{destination.stem}.ass"
    build_clip_ass(metadata, start, end, ass_file, width, height)
    safe_title = title.replace("'", "’").replace(":", " - ")[:90]
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    vf = (
        f"drawtext=fontfile={font}:text='PATCH NOTES\\: AMERICA':fontcolor=white:fontsize={max(34,int(width*.045))}:x=(w-text_w)/2:y={int(height*.07)},"
        f"drawtext=fontfile={font}:text='{safe_title}':fontcolor=white:fontsize={max(28,int(width*.038))}:x=(w-text_w)/2:y={int(height*.12)}:box=1:boxcolor=black@0.35:boxborderw=18,"
        f"subtitles='{ass_file.as_posix()}':fontsdir=/usr/share/fonts/truetype/dejavu"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x0d1117:s={width}x{height}:r=30:d={duration}",
        "-ss", str(start), "-t", str(duration), "-i", str(mp3_files[0]),
        "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(destination),
    ]
    subprocess.run(cmd, check=True)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "default_voice": DEFAULT_VOICE,
            "default_tempo": DEFAULT_TEMPO,
            "host_profiles": load_host_profiles(),
            "chatterbox_public_port": CHATTERBOX_PUBLIC_PORT,
        },
    )


@app.get("/api/host-profiles")
async def get_host_profiles():
    return {"hosts": load_host_profiles()}


@app.get("/api/chatterbox-voices")
async def get_chatterbox_voices():
    try:
        voices = await chatterbox_voices()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not load Chatterbox voices: {exc}") from exc
    return {"voices": voices}


@app.post("/api/host-profiles")
async def update_host_profiles(hosts_json: str = Form(...)):
    hosts = parse_hosts(hosts_json)
    save_host_profiles(hosts)
    return {"ok": True, "hosts": hosts, "count": len(hosts)}


@app.get("/api/saved-episodes")
async def list_saved_episodes():
    episodes = []
    for path in sorted(SAVED_EPISODES_DIR.glob("*/episode.json"), reverse=True):
        try:
            episode = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        episodes.append({
            "id": path.parent.name,
            "title": episode.get("title", "Untitled episode"),
            "updated_at": episode.get("updated_at"),
            "has_intro_track": bool(episode.get("intro_track")),
        })
    return {"episodes": episodes}


@app.get("/api/saved-episodes/{episode_id}")
async def get_saved_episode(episode_id: str):
    _, episode = _load_saved_episode(episode_id)
    return {"id": episode_id, "episode": episode}


@app.post("/api/saved-episodes")
async def save_episode(
    episode_json: str = Form(...),
    episode_id: str = Form(""),
    remove_intro_track: bool = Form(False),
    intro_track: UploadFile | None = File(None),
):
    episode = parse_saved_episode(episode_json)
    now = datetime.now(timezone.utc).isoformat()
    if episode_id:
        episode_dir, previous = _load_saved_episode(episode_id)
        saved_id = episode_id
        episode["created_at"] = previous.get("created_at", now)
        if previous.get("intro_track") and not remove_intro_track:
            episode["intro_track"] = previous["intro_track"]
    else:
        saved_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slugify(episode['title'])}-{uuid4().hex[:8]}"
        episode_dir = SAVED_EPISODES_DIR / saved_id
        episode["created_at"] = now
    episode_dir.mkdir(parents=True, exist_ok=True)

    if remove_intro_track:
        for old_track in episode_dir.glob("intro-track.*"):
            old_track.unlink(missing_ok=True)
        episode.pop("intro_track", None)
    if intro_track and intro_track.filename:
        suffix = Path(intro_track.filename).suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}:
            raise HTTPException(status_code=400, detail="Intro track must be WAV, MP3, FLAC, M4A, AAC, or OGG")
        uploaded = await intro_track.read(MAX_INTRO_TRACK_BYTES + 1)
        if len(uploaded) > MAX_INTRO_TRACK_BYTES:
            raise HTTPException(status_code=413, detail="Intro track is too large")
        for old_track in episode_dir.glob("intro-track.*"):
            old_track.unlink(missing_ok=True)
        stored_name = f"intro-track{suffix}"
        (episode_dir / stored_name).write_bytes(uploaded)
        episode["intro_track"] = {
            "filename": Path(intro_track.filename).name,
            "stored_name": stored_name,
        }
    episode["updated_at"] = now
    (episode_dir / "episode.json").write_text(json.dumps(episode, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "id": saved_id, "episode": episode}


@app.post("/api/hosts/{host_id}/voice")
async def upload_host_voice(
    host_id: str,
    audio: UploadFile = File(...),
    exaggeration: float = Form(0.5),
    cfg_weight: float = Form(0.5),
):
    """Validate and normalize an untrusted reference recording for one host."""
    hosts, host = find_host(host_id)
    suffix = Path(audio.filename or "").suffix.lower()
    allowed = {".wav", ".mp3", ".flac", ".m4a"}
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail="Unsupported audio format. Please upload WAV, MP3, FLAC, or M4A.")
    declared_type = (audio.content_type or "").lower()
    if declared_type and not (
        declared_type.startswith("audio/")
        or declared_type in {"application/octet-stream", "application/mp4", "video/mp4"}
    ):
        raise HTTPException(status_code=415, detail="Unsupported audio format. Please upload WAV, MP3, FLAC, or M4A.")
    if not 0 <= exaggeration <= 1 or not 0 <= cfg_weight <= 1:
        raise HTTPException(status_code=400, detail="Exaggeration and CFG weight must be between 0 and 1.")

    upload = VOICE_DIR / host["id"] / f"upload{suffix}"
    upload.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with upload.open("wb") as stream:
            while chunk := await audio.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_VOICE_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Reference audio must be no larger than {MAX_VOICE_UPLOAD_BYTES // 1024 // 1024} MB.",
                    )
                stream.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="The selected audio file is empty.")
        normalize_reference_audio(upload, reference_path(host))
    finally:
        upload.unlink(missing_ok=True)
        await audio.close()

    host["reference_audio_path"] = f"voices/{host['id']}/reference.wav"
    host["reference_audio_filename"] = Path(audio.filename or "reference.wav").name
    host["voice"] = DEFAULT_VOICE
    host["exaggeration"] = exaggeration
    host["cfg_weight"] = cfg_weight
    save_host_update(hosts, host)
    return voice_metadata(host)


@app.delete("/api/hosts/{host_id}/voice")
async def remove_host_voice(host_id: str):
    hosts, host = find_host(host_id)
    reference_path(host).unlink(missing_ok=True)
    host["reference_audio_path"] = None
    host["reference_audio_filename"] = None
    host["voice"] = DEFAULT_VOICE
    save_host_update(hosts, host)
    return voice_metadata(host)


@app.get("/api/hosts/{host_id}/voice/audio")
async def play_host_voice(host_id: str):
    _, host = find_host(host_id)
    path = reference_path(host)
    if not host.get("reference_audio_path") or not path.is_file():
        raise HTTPException(status_code=404, detail="Reference voice file is missing.")
    return FileResponse(path, media_type="audio/wav", filename="reference.wav")


@app.post("/api/hosts/{host_id}/voice/preview")
async def preview_host_voice(
    host_id: str,
    exaggeration: float = Form(0.5),
    cfg_weight: float = Form(0.5),
):
    _, host = find_host(host_id)
    if not 0 <= exaggeration <= 1 or not 0 <= cfg_weight <= 1:
        raise HTTPException(status_code=400, detail="Exaggeration and CFG weight must be between 0 and 1.")
    voice = f"host-{host['id']}" if host.get("reference_audio_path") and reference_path(host).is_file() else DEFAULT_VOICE
    preview = OUTPUT_DIR / f"voice-preview-{uuid4().hex}.wav"
    try:
        await synthesize_chunk(
            "Welcome back. Let's take a closer look at what's happening today.",
            voice, host["tempo"], preview, exaggeration=exaggeration, cfg_weight=cfg_weight,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=describe_chatterbox_error(exc)) from exc
    return FileResponse(
        preview, media_type="audio/wav", filename="voice-preview.wav",
        background=BackgroundTask(preview.unlink, missing_ok=True),
    )


@app.get("/api/health")
async def health():
    chatterbox = {"ok": False}
    try:
        status = await chatterbox_status()
        chatterbox = {"ok": True, "status": status}
    except Exception as exc:  # noqa: BLE001
        chatterbox = {"ok": False, "error": str(exc)}
    return {"app": "ok", "chatterbox": chatterbox, "chatterbox_public_url": CHATTERBOX_PUBLIC_URL or None}


@app.post("/api/conversation-draft")
async def conversation_draft(
    story_notes: str = Form(...),
    hosts_json: str = Form(...),
    target_minutes: int = Form(8),
    tone: str = Form("smart, funny, conversational, politically non-tribal"),
):
    hosts = parse_hosts(hosts_json)
    prompt = build_conversation_prompt(story_notes, hosts, target_minutes=target_minutes, tone=tone)
    script = await generate_conversation(prompt)
    # Validate speaker tags before returning a script that the audio pipeline cannot render.
    parse_speaker_script(script, hosts)
    return {
        "ok": True,
        "script": script,
        "hosts": len(hosts),
        "model": conversation_model_name(),
        "provider": CONVERSATION_PROVIDER,
        "target_minutes": target_minutes,
    }


@app.post("/api/chunk-preview")
async def chunk_preview(
    script: str = Form(...),
    hosts_json: str = Form(...),
):
    hosts = parse_hosts(hosts_json)
    chunks = build_speech_chunks(script, hosts)
    return {"count": len(chunks), "hosts": hosts, "utterances": chunks}


@app.post("/api/generation-jobs", status_code=202)
async def create_generation_job(
    title: str = Form(...),
    script: str = Form(...),
    hosts_json: str = Form(...),
    intro_lines: str = Form(""),
    intro_overlap: bool = Form(False),
    intro_music_volume: float = Form(0.25),
    intro_track: UploadFile | None = File(None),
    saved_episode_id: str = Form(""),
    omit_saved_intro: bool = Form(False),
):
    """Queue an episode whose ordered speech chunks are durable render units."""
    if not script.strip():
        raise HTTPException(status_code=400, detail="Script cannot be empty")
    hosts = parse_hosts(hosts_json)
    script = remove_accidental_transcript_repetition(script)
    intro_lines = remove_intro_episode_overlap(intro_lines, script)
    speech_chunks = build_episode_speech_chunks(script, hosts, intro_lines)
    if not speech_chunks:
        raise HTTPException(status_code=400, detail="No speakable text found")
    if not 0.0 <= intro_music_volume <= 1.0:
        raise HTTPException(status_code=400, detail="Intro music volume must be between 0 and 1")
    chunks = build_generation_segments(speech_chunks)
    job_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slugify(title)}-{uuid4().hex[:8]}"
    job_dir = OUTPUT_DIR / job_id
    (job_dir / "chunks").mkdir(parents=True)
    (job_dir / "script.txt").write_text(clean_script(script), encoding="utf-8")
    intro: dict[str, Any] = {
        "lines": clean_script(intro_lines),
        "track": None,
        "track_filename": None,
        "overlap": intro_overlap,
        "music_volume": intro_music_volume,
    }
    if intro_lines.strip():
        (job_dir / "intro-lines.txt").write_text(clean_script(intro_lines), encoding="utf-8")
    saved_intro_path: Path | None = None
    saved_intro_filename: str | None = None
    if saved_episode_id and not omit_saved_intro and not (intro_track and intro_track.filename):
        saved_dir, saved_episode = _load_saved_episode(saved_episode_id)
        saved_intro = saved_episode.get("intro_track")
        if isinstance(saved_intro, dict) and saved_intro.get("stored_name"):
            candidate = saved_dir / Path(str(saved_intro["stored_name"])).name
            if candidate.is_file():
                saved_intro_path = candidate
                saved_intro_filename = Path(str(saved_intro.get("filename") or candidate.name)).name
    if intro_track and intro_track.filename:
        suffix = Path(intro_track.filename).suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Intro track must be WAV, MP3, FLAC, M4A, AAC, or OGG")
        uploaded = await intro_track.read(MAX_INTRO_TRACK_BYTES + 1)
        if len(uploaded) > MAX_INTRO_TRACK_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=413, detail="Intro track is too large")
        source = job_dir / f"intro-upload{suffix}"
        source.write_bytes(uploaded)
        try:
            normalize_intro_track(source, job_dir / "intro-track.wav")
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        finally:
            source.unlink(missing_ok=True)
        intro.update({"track": "intro-track.wav", "track_filename": Path(intro_track.filename).name})
    elif saved_intro_path:
        try:
            normalize_intro_track(saved_intro_path, job_dir / "intro-track.wav")
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        intro.update({"track": "intro-track.wav", "track_filename": saved_intro_filename})
    job = {
        "id": job_id,
        "title": title,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hosts": hosts,
        "media": ["audio"],
        "intro": intro,
        "chunks": chunks,
        "download_url": None,
        "error": None,
    }
    _write_job(job_dir, job)
    await queue_generation_job(job_id)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "chunks": len(chunks),
        "status_url": f"/api/generation-jobs/{job_id}",
    }


@app.get("/api/generation-jobs")
async def list_generation_jobs():
    """Return recent durable jobs so a browser can reconnect after being closed."""
    jobs = []
    for manifest in sorted(OUTPUT_DIR.glob("*/job.json"), reverse=True):
        try:
            job = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _migrate_job_manifest(job)
        jobs.append({
            "id": job.get("id"),
            "title": job.get("title", "Untitled episode"),
            "status": job.get("status", "unknown"),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "download_url": job.get("download_url"),
            "error": job.get("error"),
            "progress": _job_progress(job),
        })
        if len(jobs) >= 50:
            break
    return {"jobs": jobs, "workers": JOB_WORKERS}


@app.get("/api/generation-jobs/{job_id}")
async def generation_job_status(job_id: str):
    _, job = _load_job(job_id)
    job["progress"] = _job_progress(job)
    return job


@app.post("/api/generate")
async def generate(
    title: str = Form(...),
    script: str = Form(...),
    hosts_json: str = Form(...),
):
    if not script.strip():
        raise HTTPException(status_code=400, detail="Script cannot be empty")

    hosts = parse_hosts(hosts_json)
    speech_chunks = build_speech_chunks(script, hosts)
    if not speech_chunks:
        raise HTTPException(status_code=400, detail="No speakable text found")

    episode_slug = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slugify(title)}"
    episode_dir = OUTPUT_DIR / episode_slug
    chunks_dir = episode_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    cleaned = remove_accidental_transcript_repetition(script)
    (episode_dir / "script.txt").write_text(cleaned, encoding="utf-8")
    metadata = {
        "title": title,
        "hosts": hosts,
        "utterance_count": len(speech_chunks),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "utterances": speech_chunks,
    }
    (episode_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    chunk_paths: list[Path] = []
    try:
        for chunk in speech_chunks:
            chunk_path = chunks_dir / f"utterance-{chunk['id']}.wav"
            await synthesize_chunk(
                chunk["text"], chunk["voice"], chunk["tempo"], chunk_path,
                **{field: chunk.get(field, default) for field, default in CHATTERBOX_DEFAULTS.items()},
            )
            normalized_path = normalized_chunk_path(chunk_path)
            metrics = normalize_audio_segment(chunk_path, normalized_path)
            valid, reason = validate_audio_segment(normalized_path)
            if not valid:
                normalized_path.unlink(missing_ok=True)
                raise OSError(reason)
            chunk["audio_metrics"] = metrics
            chunk["raw_output"] = chunk_path.relative_to(episode_dir).as_posix()
            chunk["normalized_output"] = normalized_path.relative_to(episode_dir).as_posix()
            chunk_paths.append(normalized_path)

        final_path = episode_dir / f"{slugify(title)}.mp3"
        timeline_wav = episode_dir / "final-audio-qa.wav"
        timeline = concatenate_wav_segments(chunk_paths, speech_chunks, timeline_wav)
        qa = analyze_final_audio_silence(timeline_wav)
        for item, chunk in zip(timeline, speech_chunks):
            metrics = chunk["audio_metrics"]
            logger.info(
                "Segment %s | Speaker: %s | Text length: %s | Raw duration: %.3fs | "
                "Leading silence: %.3fs | Trailing silence: %.3fs | Configured pause: %.3fs | "
                "Final segment duration: %.3fs | Effective gap before next speech: %.3fs | "
                "Timeline: %.3f-%.3fs",
                item["index"], item["speaker"], item["text_length"], metrics["raw_duration"],
                metrics["raw_leading_silence"], metrics["raw_trailing_silence"],
                item["pause_after_ms"] / 1000, item["duration"],
                float(metrics.get("trailing_silence", 0)) + item["pause_after_ms"] / 1000,
                item["start"], item["end"],
            )
        for region in qa:
            logger.warning("Audio QA: %.3f - %.3f | Silence: %.3f seconds",
                           region["start"], region["end"], region["duration"])
        assemble_mp3(chunk_paths, final_path, speech_chunks)
        for target, item in zip(metadata["utterances"], timeline):
            target["timing"] = {key: round(float(item[key]), 3) for key in ("duration", "start", "end")}
            target["transition"]["pause_after_ms"] = item["pause_after_ms"]
        metadata["duration"] = round(timeline[-1]["end"] + timeline[-1]["pause_after_ms"] / 1000, 3)
        metadata["audio_qa"] = qa
        timeline_wav.unlink(missing_ok=True)
        (episode_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except httpx.HTTPError as exc:
        failed_chunk = len(chunk_paths) + 1
        detail = describe_chatterbox_error(exc)
        raise HTTPException(
            status_code=502,
            detail=f"Chatterbox TTS request failed on chunk {failed_chunk} of {len(speech_chunks)}: {detail}",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f"Audio assembly failed: {exc}") from exc

    return JSONResponse(
        {
            "ok": True,
            "episode": episode_slug,
            "hosts": len(hosts),
            "chunks": len(speech_chunks),
            "duration": metadata.get("duration"),
            "download_url": f"/api/episodes/{episode_slug}/download",
            "clip_studio_url": f"/api/episodes/{episode_slug}/clip-suggestions",
        }
    )


@app.get("/api/episodes/{episode_slug}/clip-suggestions")
async def clip_suggestions(episode_slug: str):
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", episode_slug):
        raise HTTPException(status_code=400, detail="Invalid episode id")
    episode_dir = OUTPUT_DIR / episode_slug
    metadata_file = episode_dir / "metadata.json"
    if not metadata_file.exists():
        raise HTTPException(status_code=404, detail="Episode not found")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    if any("start" not in c.get("timing", c) for c in ordered_utterances(metadata)):
        metadata = enrich_chunk_timeline(metadata, episode_dir)
        metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return {"episode": episode_slug, "duration": metadata.get("duration"), "suggestions": suggest_clip_windows(metadata)}


@app.post("/api/episodes/{episode_slug}/clips")
async def create_clip(
    episode_slug: str,
    start: float = Form(...),
    end: float = Form(...),
    clip_title: str = Form("Best moment"),
    aspect: str = Form("vertical"),
):
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", episode_slug):
        raise HTTPException(status_code=400, detail="Invalid episode id")
    episode_dir = OUTPUT_DIR / episode_slug
    metadata_file = episode_dir / "metadata.json"
    if not metadata_file.exists():
        raise HTTPException(status_code=404, detail="Episode not found")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    if any("start" not in c.get("timing", c) for c in ordered_utterances(metadata)):
        metadata = enrich_chunk_timeline(metadata, episode_dir)
        metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    duration = float(metadata.get("duration") or 0)
    if start < 0 or end <= start or (duration and end > duration + 0.25):
        raise HTTPException(status_code=400, detail="Clip start/end are outside the episode timeline")
    clip_id = f"{slugify(clip_title)}-{int(start*1000)}-{int(end*1000)}-{aspect}"
    destination = episode_dir / "clips" / f"{clip_id}.mp4"
    destination.parent.mkdir(exist_ok=True)
    try:
        render_social_clip(episode_dir, start, end, clip_title, aspect, destination, metadata)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f"Clip rendering failed: {exc}") from exc
    return {
        "ok": True, "clip": clip_id, "start": start, "end": end, "aspect": aspect,
        "download_url": f"/api/episodes/{episode_slug}/clips/{clip_id}/download",
    }


@app.get("/api/episodes/{episode_slug}/clips/{clip_id}/download")
async def download_clip(episode_slug: str, clip_id: str):
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", episode_slug) or not re.fullmatch(r"[a-zA-Z0-9._-]+", clip_id):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    path = OUTPUT_DIR / episode_slug / "clips" / f"{clip_id}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/episodes/{episode_slug}/download")
async def download_episode(episode_slug: str):
    episode_dir = OUTPUT_DIR / episode_slug
    if not episode_dir.exists():
        raise HTTPException(status_code=404, detail="Episode not found")
    mp3_files = list(episode_dir.glob("*.mp3"))
    if not mp3_files:
        raise HTTPException(status_code=404, detail="Episode audio not found")
    return FileResponse(mp3_files[0], media_type="audio/mpeg", filename=mp3_files[0].name)

RESEARCH_PACKETS_DIR = CONFIG_DIR / "research_packets"
RESEARCH_PACKETS_DIR.mkdir(parents=True, exist_ok=True)


def parse_research_packet(packet_json: str) -> dict[str, Any]:
    try:
        packet = json.loads(packet_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Research packet is invalid JSON") from exc
    if not isinstance(packet, dict):
        raise HTTPException(status_code=400, detail="Research packet must be an object")
    title = _text_field(packet, "title", "Untitled episode")
    raw_stories = packet.get("stories", [])
    if not isinstance(raw_stories, list) or not raw_stories:
        raise HTTPException(status_code=400, detail="Research packet needs at least one story")
    stories = []
    for i, story in enumerate(raw_stories, 1):
        if not isinstance(story, dict):
            raise HTTPException(status_code=400, detail=f"Story {i} is invalid")
        headline = _text_field(story, "headline")
        facts = _text_field(story, "verified_facts")
        if not headline or not facts:
            raise HTTPException(status_code=400, detail=f"Story {i} needs a headline and verified facts")
        sources = story.get("sources", [])
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.splitlines() if s.strip()]
        if not isinstance(sources, list):
            raise HTTPException(status_code=400, detail=f"Sources for story {i} must be a list")
        stories.append({
            "headline": headline,
            "importance": _text_field(story, "importance", "medium").lower(),
            "verified_facts": facts,
            "disputed_or_uncertain": _text_field(story, "disputed_or_uncertain"),
            "angles": _text_field(story, "angles"),
            "sources": [str(s).strip() for s in sources if str(s).strip()],
        })
    return {"title": title, "episode_angle": _text_field(packet, "episode_angle"), "stories": stories}


def research_packet_to_notes(packet: dict[str, Any]) -> str:
    lines = [f"EPISODE: {packet['title']}"]
    if packet.get("episode_angle"):
        lines += [f"EPISODE ANGLE: {packet['episode_angle']}"]
    for i, story in enumerate(packet["stories"], 1):
        lines += ["", f"STORY {i}: {story['headline']}", f"Importance: {story['importance']}",
                  "Verified facts:", story["verified_facts"]]
        if story.get("disputed_or_uncertain"):
            lines += ["Disputed / uncertain:", story["disputed_or_uncertain"]]
        if story.get("angles"):
            lines += ["Discussion angles:", story["angles"]]
        if story.get("sources"):
            lines += ["Sources:"] + [f"- {s}" for s in story["sources"]]
    return "\n".join(lines).strip()


@app.post("/api/research-packets")
async def save_research_packet(packet_json: str = Form(...)):
    packet = parse_research_packet(packet_json)
    packet_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slugify(packet['title'])}"
    path = RESEARCH_PACKETS_DIR / f"{packet_id}.json"
    path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "id": packet_id, "packet": packet, "notes": research_packet_to_notes(packet)}


@app.get("/api/research-packets")
async def list_research_packets():
    packets = []
    for path in sorted(RESEARCH_PACKETS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            packets.append({"id": path.stem, "title": data.get("title", path.stem), "stories": len(data.get("stories", []))})
        except (OSError, json.JSONDecodeError):
            continue
    return {"packets": packets}


@app.get("/api/research-packets/{packet_id}")
async def get_research_packet(packet_id: str):
    safe_id = slugify(packet_id)
    path = RESEARCH_PACKETS_DIR / f"{safe_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Research packet not found")
    packet = json.loads(path.read_text(encoding="utf-8"))
    return {"id": safe_id, "packet": packet, "notes": research_packet_to_notes(packet)}


@app.post("/api/conversation-draft-packet")
async def conversation_draft_packet(
    packet_json: str = Form(...), hosts_json: str = Form(...), target_minutes: int = Form(30),
    tone: str = Form("smart, funny, conversational, politically non-tribal"),
):
    packet = parse_research_packet(packet_json)
    hosts = parse_hosts(hosts_json)
    notes = research_packet_to_notes(packet)
    prompt = build_conversation_prompt(notes, hosts, target_minutes=target_minutes, tone=tone)
    prompt += "\n\nEPISODE FLOW RULES\n- Cover every story in the packet.\n- Spend more time on high-importance stories.\n- Use natural transitions and callbacks between stories.\n- Clearly distinguish verified facts from disputed or uncertain claims.\n- Do not read source URLs aloud.\n"
    script = await generate_conversation(prompt)
    parse_speaker_script(script, hosts)
    return {"ok": True, "script": script, "hosts": len(hosts), "stories": len(packet["stories"]), "model": conversation_model_name(), "provider": CONVERSATION_PROVIDER}
