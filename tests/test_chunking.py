from app.main import clean_script, split_script


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
