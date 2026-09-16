from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_codex_publish_workflow_is_deterministic_and_non_recursive():
    path = ROOT / ".github" / "workflows" / "publish-codex-report.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    assert "branches: [main]" in text
    assert '"codex-output/*.json"' in text
    assert "codex-publish-check" in text
    assert text.find("codex-publish-check") < text.find("Rebuild Pages")
    assert "git diff --name-only" in text
    assert "git add site" in text
    assert '- "site/"' not in text
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["notify"]["needs"] == ["prepare", "deploy"]
    assert workflow["jobs"]["notify"]["if"] == "always() && needs.prepare.result == 'success'"


def test_codex_publish_workflow_has_no_model_or_retrieval_calls_and_uses_secret_expressions():
    text = (ROOT / ".github" / "workflows" / "publish-codex-report.yml").read_text(encoding="utf-8")
    lowered = text.casefold()

    for forbidden in ("openai", "deepseek", "client_from_env", "collect_candidates", "pubmed", "crossref", "radar_triage_model", "radar_review_model"):
        assert forbidden not in lowered
    for name in ("WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_OPEN_ID", "WECHAT_TEMPLATE_ID"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in text


def test_deepseek_workflow_is_manual_fallback_and_pr_ci_remains_available():
    text = (ROOT / ".github" / "workflows" / "daily-radar.yml").read_text(encoding="utf-8")

    assert "name: PYGL Research Radar DeepSeek fallback" in text
    assert "workflow_dispatch:" in text
    assert "schedule:" not in text
    assert "pull_request:" in text
