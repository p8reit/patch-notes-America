import math
import struct
import wave
from types import SimpleNamespace

import pytest

from app.audio_utils import (
    SPEAKER_CHANGE_PAUSE_MS,
    SAME_SPEAKER_PAUSE_MS,
    analyze_final_audio_silence,
    calculate_transition_pause,
    detect_boundary_silence,
    is_speakable_text,
    normalize_audio_segment,
    trim_boundary_silence,
    validate_audio_segment,
)
from app.main import mix_intro_track_and_voice, parse_speaker_script


RATE = 8000


def write_wav(path, parts):
    samples = []
    for duration, audible in parts:
        for index in range(round(duration * RATE)):
            value = int(12000 * math.sin(2 * math.pi * 220 * index / RATE)) if audible else 0
            samples.append(value)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", value) for value in samples))


def hosts():
    return [{"name": "Wade Mercer"}, {"name": "Marcus Reed"}, {"name": "Julian Cross"}]


def test_trims_two_seconds_leading_silence(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    write_wav(source, [(2, False), (0.5, True)])
    trim_boundary_silence(source, output)
    assert detect_boundary_silence(output)["leading_silence"] == pytest.approx(0.25, abs=0.002)


def test_trims_three_seconds_trailing_silence(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    write_wav(source, [(0.5, True), (3, False)])
    trim_boundary_silence(source, output)
    assert detect_boundary_silence(output)["trailing_silence"] == pytest.approx(0.35, abs=0.002)


def test_keeps_normal_boundary_silence(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    write_wav(source, [(0.2, False), (0.5, True), (0.2, False)])
    trim_boundary_silence(source, output)
    assert detect_boundary_silence(output)["duration"] == pytest.approx(0.9, abs=0.002)


def test_normalization_preserves_original_generated_audio(tmp_path):
    source, output = tmp_path / "generated.wav", tmp_path / "normalized" / "generated.wav"
    write_wav(source, [(2, False), (0.5, True), (3, False)])
    original = source.read_bytes()

    normalize_audio_segment(source, output)

    assert source.read_bytes() == original
    assert detect_boundary_silence(source)["duration"] == pytest.approx(5.5, abs=0.002)
    assert detect_boundary_silence(output)["duration"] == pytest.approx(1.1, abs=0.002)


def test_rejects_empty_wav(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
    assert validate_audio_segment(path)[0] is False


def test_rejects_silent_wav(tmp_path):
    path = tmp_path / "silent.wav"
    write_wav(path, [(1, False)])
    assert validate_audio_segment(path) == (False, "WAV contains no audible speech")


def test_same_speaker_transition():
    assert calculate_transition_pause({"host": "Wade"}, {"host": "Wade"}) == SAME_SPEAKER_PAUSE_MS


def test_different_speaker_transition():
    assert calculate_transition_pause({"host": "Wade"}, {"host": "Marcus"}) == SPEAKER_CHANGE_PAUSE_MS


def test_multiple_consecutive_speaker_tags_do_not_create_empty_segments():
    result = parse_speaker_script("[Marcus Reed]\n[Julian Cross]\nHello.", hosts())
    assert result == [{"host": "Julian Cross", "text": "Hello."}]


def test_punctuation_only_is_not_speakable():
    assert is_speakable_text(" ... !!! — ") is False


def test_final_audio_reports_long_silence(tmp_path):
    path = tmp_path / "final.wav"
    write_wav(path, [(0.5, True), (2.1, False), (0.5, True)])
    regions = analyze_final_audio_silence(path)
    assert len(regions) == 1
    assert regions[0]["duration"] == pytest.approx(2.1, abs=0.002)


def test_intro_music_and_voice_are_mixed_for_the_longer_input(monkeypatch, tmp_path):
    music = tmp_path / "music.wav"
    voice = tmp_path / "voice.wav"
    mixed = tmp_path / "mixed.wav"
    write_wav(music, [(1.0, True)])
    write_wav(voice, [(0.5, True)])

    command = []

    def fake_run(args, **kwargs):
        command.extend(args)
        write_wav(tmp_path / "mixed.wav.tmp", [(1.0, True)])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("app.main.subprocess.run", fake_run)
    mix_intro_track_and_voice(music, voice, mixed, music_volume=0.2)

    info = detect_boundary_silence(mixed)
    assert info["duration"] == pytest.approx(1.0, abs=0.01)
    assert info["silent"] is False
    audio_filter = command[command.index("-filter_complex") + 1]
    assert "volume=0.200" in audio_filter
    assert "duration=longest" in audio_filter


def test_intro_music_volume_must_be_in_range(tmp_path):
    with pytest.raises(ValueError, match="between 0 and 1"):
        mix_intro_track_and_voice(
            tmp_path / "music.wav", tmp_path / "voice.wav", tmp_path / "mixed.wav", 1.1
        )
