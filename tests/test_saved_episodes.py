import json

from fastapi.testclient import TestClient

import app.main as main


def episode_document():
    return {
        "title": "Election Night Notes",
        "hosts": [{
            "id": "a" * 32,
            "name": "Alex",
            "voice": "default",
            "tempo": 1.0,
        }],
        "research_packet": {
            "episode_angle": "What changed?",
            "stories": [{"headline": "A result", "verified_facts": "The count is final."}],
        },
        "story_notes": "Producer notes",
        "target_minutes": 12,
        "conversation_tone": "measured",
        "script": "[Alex]\nWelcome to the show.",
        "intro_lines": "Tonight, we explain the result.",
        "intro_overlap": True,
        "intro_music_volume": 0.35,
    }


def test_parse_saved_episode_preserves_all_editor_sections():
    episode = main.parse_saved_episode(json.dumps(episode_document()))

    assert episode["title"] == "Election Night Notes"
    assert episode["hosts"][0]["name"] == "Alex"
    assert episode["research_packet"]["stories"][0]["headline"] == "A result"
    assert episode["story_notes"] == "Producer notes"
    assert episode["script"].startswith("[Alex]")
    assert episode["intro_overlap"] is True
    assert episode["intro_music_volume"] == 0.35


def test_saved_episode_api_round_trip_includes_intro_track(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SAVED_EPISODES_DIR", tmp_path)
    client = TestClient(main.app)

    response = client.post(
        "/api/saved-episodes",
        data={"episode_json": json.dumps(episode_document())},
        files={"intro_track": ("theme.mp3", b"audio bytes", "audio/mpeg")},
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["episode"]["intro_track"]["filename"] == "theme.mp3"
    episode_id = saved["id"]
    assert (tmp_path / episode_id / "intro-track.mp3").read_bytes() == b"audio bytes"

    loaded = client.get(f"/api/saved-episodes/{episode_id}").json()
    assert loaded["episode"]["script"] == episode_document()["script"]
    listing = client.get("/api/saved-episodes").json()["episodes"]
    assert listing == [{
        "id": episode_id,
        "title": "Election Night Notes",
        "updated_at": saved["episode"]["updated_at"],
        "has_intro_track": True,
    }]


def test_updating_saved_episode_can_remove_intro_track(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SAVED_EPISODES_DIR", tmp_path)
    client = TestClient(main.app)
    created = client.post(
        "/api/saved-episodes",
        data={"episode_json": json.dumps(episode_document())},
        files={"intro_track": ("theme.wav", b"wave", "audio/wav")},
    ).json()

    response = client.post("/api/saved-episodes", data={
        "episode_json": json.dumps(episode_document()),
        "episode_id": created["id"],
        "remove_intro_track": "true",
    })

    assert response.status_code == 200
    assert "intro_track" not in response.json()["episode"]
    assert not list((tmp_path / created["id"]).glob("intro-track.*"))
