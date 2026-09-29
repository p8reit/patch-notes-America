import subprocess
from pathlib import Path


def test_primary_producer_browser_journey():
    result = subprocess.run(
        ["node", "--test", "tests/browser/producer_journey.test.js"],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
