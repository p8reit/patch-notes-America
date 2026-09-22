import httpx
import pytest

from app.main import MAX_CHARS, build_generation_segments, clean_script, describe_chatterbox_error, split_script


def test_clean_script_removes_basic_markdown():
    raw = "## TITLE\n\n**Hello** world."
    assert clean_script(raw) == "TITLE\n\nHello world."


def test_split_script_respects_limit():
    text = "First sentence. " * 100
    chunks = split_script(text, max_chars=120)
    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_default_chunks_stay_within_chatterbox_safe_input_window():
    chunks = split_script("This is a complete spoken sentence. " * 100)

    assert MAX_CHARS == 280
    assert all(len(chunk) <= 280 for chunk in chunks)


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


def test_generation_chunks_are_ordered_durable_units():
    chunks = [
        {"host": "Wade", "voice": "am_michael", "tempo": 1.0, "text": f"Part {number}"}
        for number in range(5)
    ]

    durable_chunks = build_generation_segments(chunks)

    assert [chunk["id"] for chunk in durable_chunks] == [f"chunk-{number:03d}" for number in range(1, 6)]
    assert [chunk["number"] for chunk in durable_chunks] == list(range(1, 6))
    assert all(chunk["status"] == "queued" for chunk in durable_chunks)


def test_job_progress_counts_completed_chunks():
    from app.main import _job_progress

    job = {"chunks": [{"status": "complete"}, {"status": "running"}, {"status": "queued"}]}

    assert _job_progress(job) == {
        "complete": 1,
        "total": 3,
    }


def test_generation_chunks_initialize_all_durable_state():
    chunks = [{"host": "Wade", "voice": "am_michael", "tempo": 1.0, "text": "Hello"}]

    chunk = build_generation_segments(chunks)[0]

    assert chunk["status"] == "queued"
    assert chunk["attempt"] == 0
    assert chunk["output"] is None
    assert chunk["normalized_output"] is None
    assert chunk["audio_metrics"] is None
    assert chunk["error"] is None


def test_legacy_segment_manifest_is_migrated_in_order():
    from app.main import _migrate_job_manifest

    job = {
        "segments": [
            {"chunks": [{"text": "one", "status": "complete", "attempt": 2, "output": "one.wav"}]},
            {"chunks": [{"text": "two", "error": "interrupted"}]},
        ]
    }

    assert _migrate_job_manifest(job) is True
    assert job["chunks"][0]["status"] == "complete"
    assert job["chunks"][0]["output"] == "one.wav"
    assert job["chunks"][1]["status"] == "queued"
    assert job["chunks"][1]["output"] is None


def test_chunk_retry_checkpoints_and_preserves_completed_wav(tmp_path, monkeypatch):
    import asyncio

    from app import main

    attempts = 0

    async def flaky_synthesis(text, voice, tempo, destination, **settings):
        import struct
        import wave

        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("backend restarted")
        with wave.open(str(destination), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(b"".join(struct.pack("<h", 10000) for _ in range(800)))

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(main, "synthesize_chunk", flaky_synthesis)
    monkeypatch.setattr(main.asyncio, "sleep", no_delay)
    monkeypatch.setattr(main, "TTS_MAX_ATTEMPTS", 3)
    job = {"chunks": []}
    chunk = {"text": "Hello", "voice": "am_michael", "tempo": 1.0}
    destination = tmp_path / "chunk.wav"

    asyncio.run(main.synthesize_chunk_with_retry(tmp_path, job, chunk, destination))

    assert attempts == 3
    assert chunk["status"] == "complete"
    assert chunk["attempt"] == 3
    assert chunk["output"] == "chunk.wav"
    assert chunk["normalized_output"] == "normalized/chunk.wav"
    assert destination.read_bytes().startswith(b"RIFF")
    assert (tmp_path / chunk["normalized_output"]).is_file()


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
