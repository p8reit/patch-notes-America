import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app import main


def _valid_packet(**extra):
    return {
        "title": "Saved packet",
        "episode_angle": "The through line",
        "stories": [{"headline": "A story", "verified_facts": "A verified fact."}],
        **extra,
    }


def test_list_research_packets_skips_malformed_stored_packets(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RESEARCH_PACKETS_DIR", tmp_path)
    (tmp_path / "valid.json").write_text(main.json.dumps(_valid_packet()), encoding="utf-8")
    (tmp_path / "invalid-json.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "invalid-shape.json").write_text('{"title": "Broken", "stories": "nope"}', encoding="utf-8")

    response = TestClient(main.app).get("/api/research-packets")

    assert response.status_code == 200
    assert response.json() == {"packets": [{"id": "valid", "title": "Saved packet", "stories": 1}]}


def test_get_research_packet_reports_malformed_stored_packet(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RESEARCH_PACKETS_DIR", tmp_path)
    (tmp_path / "broken.json").write_text('{"title": "Broken", "stories": []}', encoding="utf-8")

    response = TestClient(main.app).get("/api/research-packets/broken")

    assert response.status_code == 422
    assert response.json() == {"detail": "Stored research packet is malformed"}


def test_get_research_packet_preserves_existing_and_unknown_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RESEARCH_PACKETS_DIR", tmp_path)
    packet = _valid_packet(editor_metadata={"color": "blue"})
    (tmp_path / "compatible.json").write_text(main.json.dumps(packet), encoding="utf-8")

    response = TestClient(main.app).get("/api/research-packets/compatible")

    assert response.status_code == 200
    assert response.json()["packet"] == packet


def test_research_packet_browser_interactions():
    repository = Path(__file__).parents[1]
    result = subprocess.run(
        ["node", "--test", "tests/browser/research_packets.test.js"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
