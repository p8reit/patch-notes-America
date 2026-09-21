"""Reusable PCM-WAV boundary normalization and podcast pacing utilities."""

from __future__ import annotations

import math
import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


SILENCE_THRESHOLD_DB = float(os.getenv("SILENCE_THRESHOLD_DB", "-45"))
SILENCE_WINDOW_MS = int(os.getenv("SILENCE_WINDOW_MS", "10"))
MIN_SPEECH_ACTIVITY_MS = int(os.getenv("MIN_SPEECH_ACTIVITY_MS", "30"))
MAX_LEADING_SILENCE_MS = int(os.getenv("MAX_LEADING_SILENCE_MS", "250"))
MAX_TRAILING_SILENCE_MS = int(os.getenv("MAX_TRAILING_SILENCE_MS", "350"))
SPEECH_SAFETY_BUFFER_MS = int(os.getenv("SPEECH_SAFETY_BUFFER_MS", "40"))
SAME_SPEAKER_PAUSE_MS = int(os.getenv("SAME_SPEAKER_PAUSE_MS", "225"))
NORMAL_TRANSITION_PAUSE_MS = int(os.getenv("NORMAL_TRANSITION_PAUSE_MS", "400"))
TECHNICAL_CONTINUATION_PAUSE_MS = int(os.getenv("TECHNICAL_CONTINUATION_PAUSE_MS", "0"))
SPEAKER_CHANGE_PAUSE_MS = int(os.getenv("SPEAKER_CHANGE_PAUSE_MS", "500"))
SECTION_CHANGE_PAUSE_MS = int(os.getenv("SECTION_CHANGE_PAUSE_MS", "600"))
DRAMATIC_PAUSE_MS = int(os.getenv("DRAMATIC_PAUSE_MS", "1000"))


@dataclass(frozen=True)
class WavData:
    sample_rate: int
    channels: int
    sample_width: int
    frames: bytes

    @property
    def frame_count(self) -> int:
        size = self.channels * self.sample_width
        return len(self.frames) // size if size else 0

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate if self.sample_rate else 0.0


def _read_wav(path: Path) -> WavData:
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("compressed WAV is not supported")
        return WavData(
            source.getframerate(), source.getnchannels(), source.getsampwidth(),
            source.readframes(source.getnframes()),
        )


def _window_rms(frames: bytes, sample_width: int) -> float:
    """Return RMS amplitude for an interleaved PCM window."""
    if not frames:
        return 0.0
    if sample_width == 1:
        samples = (value - 128 for value in frames)
        count = len(frames)
    else:
        samples = (
            int.from_bytes(frames[index:index + sample_width], "little", signed=True)
            for index in range(0, len(frames), sample_width)
        )
        count = len(frames) // sample_width
    return math.sqrt(sum(sample * sample for sample in samples) / max(count, 1))


def _frame_peak(frame: bytes, sample_width: int) -> int:
    """Return the greatest absolute PCM sample amplitude in one audio frame."""
    if sample_width == 1:
        return max((abs(value - 128) for value in frame), default=0)
    return max(
        (
            abs(int.from_bytes(frame[index:index + sample_width], "little", signed=True))
            for index in range(0, len(frame), sample_width)
        ),
        default=0,
    )


def _speech_window_bounds(
    audio: WavData, threshold: float
) -> tuple[int, int] | None:
    """Find sustained speech using short RMS windows, ignoring isolated clicks/noise."""
    frame_size = audio.channels * audio.sample_width
    window_frames = max(1, round(audio.sample_rate * SILENCE_WINDOW_MS / 1000))
    minimum_windows = max(1, math.ceil(MIN_SPEECH_ACTIVITY_MS / SILENCE_WINDOW_MS))
    active: list[bool] = []
    for start in range(0, audio.frame_count, window_frames):
        end = min(start + window_frames, audio.frame_count)
        window = audio.frames[start * frame_size:end * frame_size]
        active.append(_window_rms(window, audio.sample_width) > threshold)

    first: int | None = None
    run = 0
    for index, is_active in enumerate(active):
        run = run + 1 if is_active else 0
        if run >= minimum_windows:
            first = index - run + 1
            break
    if first is None:
        return None

    last: int | None = None
    run = 0
    for index in range(len(active) - 1, -1, -1):
        run = run + 1 if active[index] else 0
        if run >= minimum_windows:
            last = index + run
            break
    assert last is not None
    return first * window_frames, min(last * window_frames, audio.frame_count)


def detect_boundary_silence(
    path: Path, threshold_db: float = SILENCE_THRESHOLD_DB
) -> dict[str, float | bool]:
    """Measure boundary silence, adapting when a valid recording is unusually quiet.

    ``threshold_db`` remains the preferred absolute threshold.  Some TTS backends,
    however, return correctly formed speech at a very low gain.  Treating that as
    an entirely silent file makes retrying useless, so in that case detection falls
    back to ten percent of the recording's peak.  An all-zero PCM file is still
    considered silent.
    """
    try:
        audio = _read_wav(path)
    except (OSError, EOFError, wave.Error, ValueError):
        return {"duration": 0.0, "leading_silence": 0.0, "trailing_silence": 0.0, "silent": True}
    if not audio.frames or not audio.sample_rate:
        return {"duration": 0.0, "leading_silence": 0.0, "trailing_silence": 0.0, "silent": True}
    frame_size = audio.channels * audio.sample_width
    maximum = (1 << (audio.sample_width * 8 - 1)) - 1 if audio.sample_width > 1 else 127
    peaks = [_frame_peak(audio.frames[i:i + frame_size], audio.sample_width)
             for i in range(0, len(audio.frames), frame_size)]
    recording_peak = max(peaks, default=0)
    if recording_peak == 0:
        return {"duration": audio.duration_seconds, "leading_silence": audio.duration_seconds,
                "trailing_silence": audio.duration_seconds, "silent": True}
    configured_threshold = maximum * math.pow(10.0, threshold_db / 20.0)
    threshold = min(configured_threshold, recording_peak * 0.1)
    bounds = _speech_window_bounds(audio, threshold)
    if bounds is None:
        return {"duration": audio.duration_seconds, "leading_silence": audio.duration_seconds,
                "trailing_silence": audio.duration_seconds, "silent": True}
    first, speech_end = bounds
    return {
        "duration": audio.duration_seconds,
        "leading_silence": first / audio.sample_rate,
        "trailing_silence": (audio.frame_count - speech_end) / audio.sample_rate,
        "silent": False,
    }


def trim_boundary_silence(
    source: Path,
    destination: Path,
    max_leading_ms: int = MAX_LEADING_SILENCE_MS,
    max_trailing_ms: int = MAX_TRAILING_SILENCE_MS,
    safety_buffer_ms: int = SPEECH_SAFETY_BUFFER_MS,
) -> dict[str, float | bool]:
    """Cap boundary silence while preserving a buffer around quiet speech edges."""
    info = detect_boundary_silence(source)
    if info["silent"]:
        raise ValueError("WAV contains no audible speech")
    audio = _read_wav(source)
    leading = int(float(info["leading_silence"]) * audio.sample_rate)
    trailing = int(float(info["trailing_silence"]) * audio.sample_rate)
    safety = round(safety_buffer_ms * audio.sample_rate / 1000)
    keep_leading = max(safety, round(max_leading_ms * audio.sample_rate / 1000))
    keep_trailing = max(safety, round(max_trailing_ms * audio.sample_rate / 1000))
    start = max(0, leading - keep_leading)
    end = audio.frame_count - max(0, trailing - keep_trailing)
    frame_size = audio.channels * audio.sample_width
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as target:
        target.setparams((audio.channels, audio.sample_width, audio.sample_rate, 0, "NONE", "not compressed"))
        target.writeframes(audio.frames[start * frame_size:end * frame_size])
    result = detect_boundary_silence(destination)
    result["raw_duration"] = info["duration"]
    result["raw_leading_silence"] = info["leading_silence"]
    result["raw_trailing_silence"] = info["trailing_silence"]
    return result


def validate_audio_segment(path: Path) -> tuple[bool, str]:
    try:
        info = detect_boundary_silence(path)
    except Exception as exc:  # defensive boundary for job reporting
        return False, str(exc)
    if not path.is_file() or path.stat().st_size <= 44 or not info["duration"]:
        return False, "empty or invalid WAV"
    if info["silent"]:
        return False, "WAV contains no audible speech"
    return True, ""


def normalize_audio_segment(source: Path, destination: Path) -> dict[str, float | bool]:
    """Write an assembly-ready copy without modifying the original TTS render."""
    if source.resolve() == destination.resolve():
        raise ValueError("normalized audio destination must differ from its source")
    temporary = destination.with_suffix(".wav.tmp")
    try:
        metrics = trim_boundary_silence(source, temporary)
        temporary.replace(destination)
        return metrics
    finally:
        temporary.unlink(missing_ok=True)


def is_speakable_text(text: str) -> bool:
    return bool(re.search(r"[\w]", text, flags=re.UNICODE))


def calculate_transition_pause(previous: dict[str, Any], current: dict[str, Any]) -> int:
    """Return the pause declared by the current utterance's boundary metadata."""
    pauses = {
        "technical_continuation": TECHNICAL_CONTINUATION_PAUSE_MS,
        "sentence_break": SAME_SPEAKER_PAUSE_MS,
        "paragraph_break": NORMAL_TRANSITION_PAUSE_MS,
        "speaker_change": SPEAKER_CHANGE_PAUSE_MS,
        "section_break": SECTION_CHANGE_PAUSE_MS,
        "explicit_dramatic_pause": DRAMATIC_PAUSE_MS,
    }
    reason = current.get("boundary_reason")
    if reason not in pauses:
        raise ValueError(f"Missing or unknown transition boundary reason: {reason!r}")
    return pauses[reason]


def concatenate_wav_segments(
    paths: Sequence[Path], chunks: Sequence[dict[str, Any]], destination: Path
) -> list[dict[str, float | int | str]]:
    """Concatenate canonical WAVs from timestamp zero with exactly one pause source."""
    if not paths or len(paths) != len(chunks):
        raise ValueError("audio paths and chunks must be non-empty and aligned")
    audios = [_read_wav(path) for path in paths]
    canonical = (audios[0].sample_rate, audios[0].channels, audios[0].sample_width)
    if any((a.sample_rate, a.channels, a.sample_width) != canonical for a in audios):
        raise ValueError("WAV segments must share sample rate, channel count, and sample width")
    rate, channels, width = canonical
    timeline: list[dict[str, float | int | str]] = []
    cursor = 0.0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as target:
        target.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        for index, (audio, chunk) in enumerate(zip(audios, chunks)):
            start = cursor
            target.writeframes(audio.frames)
            cursor += audio.duration_seconds
            pause_ms = calculate_transition_pause(chunk, chunks[index + 1]) if index + 1 < len(chunks) else 0
            end = cursor
            timeline.append({"index": index + 1, "speaker": str(chunk.get("host", "")),
                             "text_length": len(str(chunk.get("text", ""))), "start": start,
                             "end": end, "duration": audio.duration_seconds, "pause_after_ms": pause_ms})
            if pause_ms:
                pause_frames = round(rate * pause_ms / 1000)
                target.writeframes(b"\0" * pause_frames * channels * width)
                cursor += pause_frames / rate
    return timeline


def analyze_final_audio_silence(
    path: Path, minimum_seconds: float = 1.5, threshold_db: float = SILENCE_THRESHOLD_DB
) -> list[dict[str, float]]:
    """Report (but never alter) silent regions in a PCM WAV QA artifact."""
    audio = _read_wav(path)
    frame_size = audio.channels * audio.sample_width
    window_frames = max(1, round(audio.sample_rate * SILENCE_WINDOW_MS / 1000))
    maximum = (1 << (audio.sample_width * 8 - 1)) - 1 if audio.sample_width > 1 else 127
    threshold = maximum * math.pow(10.0, threshold_db / 20.0)
    regions: list[dict[str, float]] = []
    start: int | None = None
    window_count = math.ceil(audio.frame_count / window_frames)
    for index in range(window_count + 1):
        frame_start = index * window_frames
        frame_end = min(frame_start + window_frames, audio.frame_count)
        quiet = index < window_count and _window_rms(
            audio.frames[frame_start * frame_size:frame_end * frame_size], audio.sample_width
        ) <= threshold
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            start_frame = start * window_frames
            end_frame = min(index * window_frames, audio.frame_count)
            duration = (end_frame - start_frame) / audio.sample_rate
            if duration > minimum_seconds:
                regions.append({"start": start_frame / audio.sample_rate, "end": end_frame / audio.sample_rate,
                                "duration": duration})
            start = None
    return regions
