import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app import main


ROOT = Path(__file__).parents[1]


def test_episode_json_import_browser_behavior():
    result = subprocess.run(
        ["node", "--test", "tests/browser/episode_json_import.test.js"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_imported_episode_uses_existing_save_and_load_contract(tmp_path, monkeypatch):
    result = subprocess.run(
        ["node", "-e", """
const fs = require('node:fs');
const importer = require('./app/static/episode-json-import.js');
const parsed = importer.parse(fs.readFileSync('tests/fixtures/episode-import.json', 'utf8'));
const host = {id: 'a'.repeat(32), name: 'Wade Mercer', voice: 'default', tempo: 1};
process.stdout.write(JSON.stringify(parsed.episodes.map((episode, index) => importer.toEditor(parsed, index, [host]))));
"""], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    imported_episodes = json.loads(result.stdout)
    monkeypatch.setattr(main, "SAVED_EPISODES_DIR", tmp_path)
    client = TestClient(main.app)
    saved_ids = []
    for imported in imported_episodes:
        response = client.post("/api/saved-episodes", data={"episode_json": json.dumps(imported)})
        assert response.status_code == 200
        saved = response.json()
        saved_ids.append(saved["id"])
        restored = client.get(f"/api/saved-episodes/{saved['id']}").json()["episode"]
        for key in ("title", "script", "research_packet", "story_notes", "target_minutes", "conversation_tone", "image_prompt", "intro_lines", "performance"):
            assert restored[key] == imported[key]
        assert restored["hosts"][0]["id"] == imported["hosts"][0]["id"]
        assert restored["hosts"][0]["voice"] == "default"
    assert len(set(saved_ids)) == 2
    assert len(client.get("/api/saved-episodes").json()["episodes"]) == 2
    assert imported_episodes[0]["intro_lines"] == "A little light is back."
    assert "A little light is back." not in imported_episodes[0]["script"]


def test_imported_cold_open_is_spoken_once_before_main_script():
    result = subprocess.run(
        ["node", "-e", """
const fs = require('node:fs');
const importer = require('./app/static/episode-json-import.js');
const parsed = importer.parse(fs.readFileSync('tests/fixtures/episode-import.json', 'utf8'));
const host = {id: 'a'.repeat(32), name: 'Wade Mercer', voice: 'default', tempo: 1};
process.stdout.write(JSON.stringify(importer.toEditor(parsed, 0, [host])));
"""], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    imported = json.loads(result.stdout)
    chunks = main.build_episode_speech_chunks(imported["script"], imported["hosts"], imported["intro_lines"])
    spoken = " ".join(chunk["text"] for chunk in chunks)
    assert chunks[0]["section"] == "host_intro"
    assert spoken.count("A little light is back.") == 1
    assert spoken.index("A little light is back.") < spoken.index("I'm Wade Mercer.")
