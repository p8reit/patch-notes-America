from html.parser import HTMLParser
from pathlib import Path


TEMPLATE = Path("app/templates/index.html").read_text(encoding="utf-8")


class StatusParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id") or "voice-upload-status" in values.get("class", "") or "preview-status" in values.get("class", ""):
            self.elements.append(values)


def test_all_dynamic_status_regions_are_polite_by_default():
    parser = StatusParser()
    parser.feed(TEMPLATE)
    expected_ids = {"cast-status", "voice-status", "conversation-status", "packet-status", "progress", "clip-status"}
    by_id = {item.get("id"): item for item in parser.elements if item.get("id")}
    for element_id in expected_ids:
        assert by_id[element_id]["role"] == "status"
        assert by_id[element_id]["aria-live"] == "polite"
    for class_name in ("voice-upload-status", "preview-status"):
        matches = [item for item in parser.elements if class_name in item.get("class", "").split()]
        assert matches and all(item.get("role") == "status" and item.get("aria-live") == "polite" for item in matches)


def test_accessibility_behavior_is_wired_to_requested_workflows():
    assert '<script src="/static/accessibility.js"></script>' in TEMPLATE
    assert 'aria-describedby="progress"' in TEMPLATE
    assert "a11y.focusFirstInvalid(invalidNames)" in TEMPLATE
    assert "fieldError(title, 'Enter an episode title before saving.')" in TEMPLATE
    assert "fieldError(notes, 'Add story or source notes first.')" in TEMPLATE
    assert "Enter a host name before previewing this voice." in TEMPLATE
    assert "job.error || 'Background render failed.', true" in TEMPLATE
    assert "'Correct the clip times before rendering.', true" in TEMPLATE
    assert "generateButton.title" not in TEMPLATE
