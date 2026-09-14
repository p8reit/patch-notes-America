import httpx

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
