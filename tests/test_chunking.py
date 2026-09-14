import httpx
import pytest

from app.main import build_generation_segments, clean_script, describe_kokoro_error, split_script


def test_clean_script_removes_basic_markdown():
    raw = "## TITLE\n\n**Hello** world."
    assert clean_script(raw) == "TITLE\n\nHello world."


def test_split_script_respects_limit():
    text = "First sentence. " * 100
    chunks = split_script(text, max_chars=120)
    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_split_script_keeps_short_paragraphs():
    text = "Hello there.\n\nSecond paragraph."
    chunks = split_script(text, max_chars=100)
    assert chunks == ["Hello there.\n\nSecond paragraph."]


def test_kokoro_timeout_error_is_actionable_when_exception_text_is_empty():
    request = httpx.Request("POST", "http://kokoro:7860/tts/generate")
    error = httpx.ReadTimeout("", request=request)

    detail = describe_kokoro_error(error)

    assert "did not respond within 180 seconds" in detail
    assert "http://kokoro:7860/tts/generate" in detail


def test_kokoro_http_error_includes_status_and_response_detail():
    request = httpx.Request("POST", "http://kokoro:7860/tts/generate")
    response = httpx.Response(422, text="voice is not supported", request=request)
    error = httpx.HTTPStatusError("bad response", request=request, response=response)

    detail = describe_kokoro_error(error)

    assert "HTTP 422" in detail
    assert "voice is not supported" in detail


def test_generation_segments_have_independent_media_contracts():
    chunks = [
        {"host": "Wade", "voice": "am_michael", "tempo": 1.0, "text": f"Part {number}"}
        for number in range(5)
    ]

    segments = build_generation_segments(chunks, chunks_per_segment=2)

    assert [len(segment["chunks"]) for segment in segments] == [2, 2, 1]
    assert [segment["id"] for segment in segments] == ["segment-001", "segment-002", "segment-003"]
    assert all(segment["status"] == "queued" for segment in segments)
    assert all(segment["outputs"] == {"audio": None} for segment in segments)


def test_generation_segment_size_is_validated():
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        build_generation_segments([{"text": "hello"}], chunks_per_segment=0)


def test_assemble_mp3_supports_inputs_in_sibling_directory(tmp_path, monkeypatch):
    from app import main

    chunks_dir = tmp_path / "chunks"
    segments_dir = tmp_path / "segments"
    chunks_dir.mkdir()
    segments_dir.mkdir()
    chunk_paths = [chunks_dir / "chunk-001.wav", chunks_dir / "chunk-002.wav"]
    for path in chunk_paths:
        path.touch()

    captured = {}
    monkeypatch.setattr(main.shutil, "which", lambda command: "/usr/bin/ffmpeg")

    def fake_run(command, *, cwd, check):
        captured.update(command=command, cwd=cwd, check=check)

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.assemble_mp3(chunk_paths, segments_dir / "segment-001.mp3")

    assert (segments_dir / "concat.txt").read_text() == (
        "file '../chunks/chunk-001.wav'\nfile '../chunks/chunk-002.wav'\n"
    )
    assert captured["cwd"] == segments_dir
    assert captured["check"] is True


def test_job_progress_counts_completed_segments():
    from app.main import _job_progress

    job = {"segments": [{"status": "complete"}, {"status": "running"}, {"status": "queued"}]}

    assert _job_progress(job) == {"complete": 1, "total": 3}


def test_synthesize_chunk_uses_hangrylabs_kokoro_contract(tmp_path, monkeypatch):
    import asyncio

    from app import main

    captured = {}

    class FakeResponse:
        content = b"RIFF" + b"\x00" * 40
        headers = {"content-type": "audio/wav"}
        request = httpx.Request("POST", "http://kokoro:7860/tts/generate")

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    destination = tmp_path / "speech.wav"
    asyncio.run(main.synthesize_chunk("A short transcript.", "am_michael", 1.15, destination))

    assert captured["url"] == "http://kokoro:7860/tts/generate"
    assert captured["payload"] == {
        "text": "A short transcript.",
        "voice": "am_michael",
        "speed": 1.15,
    }
    assert destination.read_bytes().startswith(b"RIFF")


def test_synthesize_chunk_rejects_json_saved_as_wav(tmp_path, monkeypatch):
    import asyncio

    from app import main

    class FakeResponse:
        content = b'{"detail":"invalid voice"}'
        text = content.decode()
        headers = {"content-type": "application/json"}
        request = httpx.Request("POST", "http://kokoro:7860/tts/generate")

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    with pytest.raises(httpx.HTTPStatusError, match="not WAV audio"):
        asyncio.run(main.synthesize_chunk("Text", "bad_voice", 1.0, tmp_path / "speech.wav"))
