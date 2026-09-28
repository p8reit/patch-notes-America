import json

from fastapi.testclient import TestClient

from app import main


def test_completed_job_reveals_clip_studio_and_targets_suggestions(monkeypatch, tmp_path):
    job_id = "completed-browser-job"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "title": "Completed browser episode",
                "status": "complete",
                "created_at": "2026-09-28T00:00:00+00:00",
                "chunks": [],
                "download_url": f"/api/episodes/{job_id}/download",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)

    client = TestClient(main.app)
    workspace = client.get("/")
    jobs = client.get("/api/generation-jobs")

    assert workspace.status_code == 200
    assert jobs.status_code == 200
    assert jobs.json()["jobs"][0]["id"] == job_id

    browser_source = workspace.text
    helper_start = browser_source.index("function setActiveCompletedEpisode(job)")
    refresh_start = browser_source.index("async function refreshJobs()")
    monitor_start = browser_source.index("function monitorJob(jobId)")
    suggestions_start = browser_source.index(
        "document.getElementById('suggest-clips').addEventListener"
    )

    helper_source = browser_source[helper_start:refresh_start]
    refresh_source = browser_source[refresh_start:monitor_start]
    monitor_source = browser_source[monitor_start:browser_source.index("document.getElementById('refresh-jobs')")]
    suggestions_source = browser_source[suggestions_start:]

    assert "document.getElementById('clip-studio').hidden = !currentEpisode" in helper_source
    assert "document.getElementById('clip-suggestions').innerHTML = ''" in helper_source
    assert "document.getElementById('clip-status').textContent = ''" in helper_source
    assert "document.getElementById('clip-result').innerHTML = ''" in helper_source
    assert "setActiveCompletedEpisode(latestComplete)" in refresh_source
    assert "setActiveCompletedEpisode(job)" in monitor_source
    assert "`/api/episodes/${currentEpisode}/clip-suggestions`" in suggestions_source
