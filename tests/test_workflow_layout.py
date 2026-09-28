from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
STYLES = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")


def test_workspace_has_six_ordered_labeled_regions_and_navigation():
    labels = [
        "Episode setup", "Cast", "Research and draft", "Script and show open",
        "Render and download", "Distribution",
    ]
    stage_ids = [f"stage-{index}" for index in range(1, 6)] + ["clip-studio"]
    positions = [TEMPLATE.index(f'id="{stage_id}"') for stage_id in stage_ids]
    assert positions == sorted(positions)
    for index, label in enumerate(labels, 1):
        target = f"stage-{index}" if index < 6 else "clip-studio"
        assert f'href="#{target}"' in TEMPLATE
        assert label in TEMPLATE
    assert 'aria-label="Episode workflow"' in TEMPLATE


def test_optional_controls_start_collapsed_without_disabling_fields():
    assert '<details class="voice-details">' in TEMPLATE
    assert '<details class="voice-details" open>' not in TEMPLATE
    assert '<details class="intro-section">' in TEMPLATE
    assert 'id="intro-lines" name="intro_lines"' in TEMPLATE
    assert 'id="clip-studio"' in TEMPLATE and 'data-workflow-stage hidden' in TEMPLATE


def test_narrow_navigation_is_single_column_and_does_not_stick_over_controls():
    mobile = STYLES[STYLES.index("@media (max-width: 700px) {", STYLES.index("/* Numbered production workflow */")):]
    assert ".workflow-nav { position: static; }" in mobile
    assert ".workflow-nav ol { grid-template-columns: 1fr; }" in mobile
    assert ":focus-visible" in STYLES


def test_completed_jobs_keep_direct_download_access_and_reveal_distribution():
    assert 'href="${encodeURI(job.download_url)}">Download MP3</a>' in TEMPLATE
    assert "document.getElementById('clip-studio').hidden = !currentEpisode" in TEMPLATE
