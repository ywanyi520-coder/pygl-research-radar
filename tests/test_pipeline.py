import json
from datetime import datetime, timezone
from pathlib import Path

from pygl_radar.notifiers import MockNotifier
from pygl_radar.models import Paper
from pygl_radar.pipeline import _within_window, run_radar


def test_previous_48_hour_gate_is_enforced():
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    assert _within_window(Paper(title="new", publication_date="2026-09-15T12:00:00+00:00"), now, 48)
    assert not _within_window(Paper(title="old", publication_date="2026-09-14T11:59:00+00:00"), now, 48)


def test_fixture_dry_run_ranks_transferable_experiment(tmp_path: Path):
    config = {
        "profile": {
            "keywords": ["PYGL", "macrophage"],
            "mechanisms": ["PYGL", "lysosome", "efferocytosis"],
            "experimental_patterns": ["conditioned medium", "size fractionation", "candidate ligand", "blockade", "add-back", "rescue", "post-engulfment"],
        },
        "triage": {"batch_size": 20, "retain": 15},
        "fulltext": {"enabled": False},
        "scoring": {"min_score": 40, "top_n": 5},
        "output": {"directory": str(tmp_path / "reports")},
        "state": {"seen_cache": str(tmp_path / "seen.json"), "feedback": str(tmp_path / "feedback.json")},
    }
    notifier = MockNotifier()
    result = run_radar(config, fixture_path=Path(__file__).parent.parent / "fixtures/sample_papers.json", dry_run=True, notifier=notifier)
    assert result.papers
    assert "Conditioned medium" in result.papers[0].title
    assert result.markdown_path.exists()
    assert json.loads(result.json_path.read_text())["papers"][0]["scores"]
    assert notifier.sent_messages
