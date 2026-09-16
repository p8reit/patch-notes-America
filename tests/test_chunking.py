import httpx
import pytest

from app.main import build_generation_segments, clean_script, describe_chatterbox_error, split_script


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


def test_chatterbox_timeout_error_is_actionable_when_exception_text_is_empty():
    request = httpx.Request("POST", "http://chatterbox:8000/v1/audio/speech")
    error = httpx.ReadTimeout("", request=request)

    detail = describe_chatterbox_error(error)

    assert "did not respond within 600 seconds" in detail
    assert "http://chatterbox:8000/v1/audio/speech" in detail


def test_chatterbox_http_error_includes_status_and_response_detail():
    request = httpx.Request("POST", "http://chatterbox:8000/v1/audio/speech")
    response = httpx.Response(422, text="voice is not supported", request=request)
    error = httpx.HTTPStatusError("bad response", request=request, response=response)

    detail = describe_chatterbox_error(error)

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


def test_job_progress_counts_completed_segments():
    from app.main import _job_progress

    job = {"segments": [{"status": "complete"}, {"status": "running"}, {"status": "queued"}]}

    assert _job_progress(job) == {
        "complete": 1,
        "total": 3,
        "chunks_complete": 0,
        "chunks_total": 0,
    }


def test_generation_segments_initialize_durable_chunk_state():
    chunks = [{"host": "Wade", "voice": "am_michael", "tempo": 1.0, "text": "Hello"}]

    segment = build_generation_segments(chunks)[0]

    assert segment["chunks"][0]["status"] == "queued"
    assert segment["chunks"][0]["output"] is None


def test_chunk_retry_checkpoints_and_preserves_completed_wav(tmp_path, monkeypatch):
    import asyncio

    from app import main

    attempts = 0

    async def flaky_synthesis(text, voice, tempo, destination, **settings):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("backend restarted")
        destination.write_bytes(b"RIFF" + b"\x00" * 44)

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(main, "synthesize_chunk", flaky_synthesis)
    monkeypatch.setattr(main.asyncio, "sleep", no_delay)
    monkeypatch.setattr(main, "TTS_MAX_ATTEMPTS", 3)
    job = {"segments": []}
    chunk = {"text": "Hello", "voice": "am_michael", "tempo": 1.0}
    destination = tmp_path / "chunk.wav"

    asyncio.run(main.synthesize_chunk_with_retry(tmp_path, job, chunk, destination))

    assert attempts == 3
    assert chunk["status"] == "complete"
    assert chunk["attempt"] == 3
    assert chunk["output"] == "chunk.wav"
    assert destination.read_bytes().startswith(b"RIFF")


def test_synthesize_chunk_uses_chatterbox_contract(tmp_path, monkeypatch):
    import asyncio

    from app import main

    captured = {}

    class FakeResponse:
        content = b"RIFF" + b"\x00" * 40
        headers = {"content-type": "audio/wav"}
        request = httpx.Request("POST", "http://chatterbox:8000/v1/audio/speech")

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

    assert captured["url"] == "http://chatterbox:8000/v1/audio/speech"
    assert captured["payload"] == {
        "input": "A short transcript.",
        "model": "chatterbox",
        "voice": "am_michael",
        "speed": 1.15,
        "response_format": "wav",
        "exaggeration": 0.5,
        "cfg_weight": 0.5,
        "temperature": 0.8,
        "min_p": 0.05,
        "top_p": 1.0,
        "repetition_penalty": 1.2,
    }
    assert destination.read_bytes().startswith(b"RIFF")


def test_chatterbox_timeout_has_a_default():
    from app import main

    assert isinstance(main.CHATTERBOX_TIMEOUT_SECONDS, float)
    assert main.CHATTERBOX_TIMEOUT_SECONDS > 0


def test_chatterbox_voices_uses_discovery_endpoint(monkeypatch):
    import asyncio

    from app import main

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"voices": [{"id": "default"}, {"id": "wade"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)

    voices = asyncio.run(main.chatterbox_voices())

    assert captured == {"timeout": 10.0, "url": "http://chatterbox:8000/voices"}
    assert voices == [{"id": "default"}, {"id": "wade"}]


def test_synthesize_chunk_rejects_json_saved_as_wav(tmp_path, monkeypatch):
    import asyncio

    from app import main

    class FakeResponse:
        content = b'{"detail":"invalid voice"}'
        text = content.decode()
        headers = {"content-type": "application/json"}
        request = httpx.Request("POST", "http://chatterbox:8000/v1/audio/speech")

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
