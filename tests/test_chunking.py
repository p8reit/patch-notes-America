import httpx

from app.main import clean_script, describe_kokoro_error, split_script


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
