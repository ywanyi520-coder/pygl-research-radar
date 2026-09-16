import json
from pathlib import Path

import yaml

from pygl_radar.pages import build_pages, public_report_url, resolve_public_site_url
from pygl_radar.pipeline import notify_report, run_radar
from pygl_radar.publishing import PublicationResult
from pygl_radar.notifiers.base import NotificationResult


def _write_report(directory: Path, date: str, *, title: str = "Transferable paper", evidence: str = "FULLTEXT_READ", raw_fulltext: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{date}.json").write_text(json.dumps({
        "version": 1,
        "date": date,
        "stats": {
            "retrieved": 12,
            "deduplicated": 8,
            "triaged": 5,
            "reviewed": 4,
            "fulltext_reviewed": 2,
            "recommended": 1,
            "issue_url": "https://github.com/example/radar/issues/9",
            "evidence": {"FULLTEXT_READ": 2, "ABSTRACT_ONLY": 2},
        },
        "papers": [{
            "title": title,
            "journal": "Nature",
            "publication_date": date,
            "doi": "10.1234/example",
            "pmid": "123456",
            "publisher_url": "https://publisher.example/paper",
            "fulltext_source_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
            "evidence_level": evidence,
            "fulltext_text": raw_fulltext,
            "final_score": 88.5,
            "scores": {
                "direct_relevance": 31,
                "mechanism_relevance": 54,
                "experimental_similarity": 91,
                "transferability": 87,
                "idea_value": 82,
                "evidence_quality": 76,
            },
            "review": {
                "core_finding": "A causal perturbation was tested with a rescue.",
                "why_recommended": "The experimental logic maps to the PYGL question.",
                "mechanism_mapping": "Upstream signal → organelle processing → function.",
                "transferable_strategy": "Blockade followed by add-back and delayed intervention.",
                "new_hypothesis": "Test a downstream bypass after acute PYGL perturbation.",
            },
        }],
    }, ensure_ascii=False), encoding="utf-8")


def test_builds_latest_archive_and_permanent_daily_pages(tmp_path: Path):
    reports = tmp_path / "reports"
    site = tmp_path / "site"
    _write_report(reports, "2026-09-15", title="Older paper")
    _write_report(reports, "2026-09-16", title="Newest paper")

    result = build_pages(reports, site, public_site_url="https://owner.github.io/pygl-research-radar")

    assert result["report_dates"] == ["2026-09-16", "2026-09-15"]
    assert (site / "index.html").exists()
    assert (site / "latest" / "index.html").exists()
    assert (site / "archive" / "index.html").exists()
    assert (site / "reports" / "2026-09-16" / "index.html").exists()
    assert (site / "reports" / "2026-09-15" / "index.html").exists()
    latest = (site / "latest" / "index.html").read_text(encoding="utf-8")
    archive = (site / "archive" / "index.html").read_text(encoding="utf-8")
    assert "2026-09-16" in latest and "Newest paper" in latest
    assert "12" in latest and "5" in latest and "2" in latest and "1" in latest
    assert "https://owner.github.io/pygl-research-radar/latest/" in latest
    assert archive.find("2026-09-16") < archive.find("2026-09-15")
    assert "Newest paper" in archive


def test_pages_preserve_history_when_a_flat_codex_report_is_added(tmp_path: Path):
    site = tmp_path / "site"
    reports = site / "data" / "reports"
    _write_report(reports, "2026-09-15", title="Existing archived report")
    codex = {
        "version": 1,
        "date": "2026-09-16",
        "review_provider": "codex-automation",
        "review_mode": "codex-native",
        "profile_digest": "fixture",
        "stats": {"candidate_pool": 72, "shortlist": 12, "recommended": 1, "evidence": {"FULLTEXT_READ": 1, "ABSTRACT_ONLY": 0}},
        "papers": [{
            "paper_id": "doi:10.1234/codex", "title": "Codex canonical paper", "journal": "Nature", "publication_date": "2026-09-16",
            "doi": "10.1234/codex", "pmid": "123", "pmcid": None, "authors": [], "publisher_url": "https://publisher.example/codex",
            "lawful_oa_urls": [], "discovery_lane": "topic", "evidence_level": "FULLTEXT_READ", "figure_evidence_mode": "RESULTS_TEXT_ONLY",
            "scores": {"direct_relevance": 30, "mechanism_relevance": 50, "experimental_similarity": 90, "transferability": 85, "idea_value": 80, "evidence_quality": 80}, "final_score": 76,
            "background": "Background.", "knowledge_gap": "Gap.", "scientific_question": "Question?", "central_hypothesis": "Hypothesis.", "study_design": "Blockade and rescue.",
            "innovations": ["Innovation."], "figure_walkthrough": [], "figure_limitations": "Images not inspected.", "key_controls_and_rescues": ["Rescue."],
            "causal_chain": "Signal to processing.", "strengths": ["Strength."], "limitations": ["Limitation."], "topic_mapping": "Map cautiously to PYGL.",
            "actionable_ideas": ["Test a bypass."], "do_not_overclaim": ["No proof."], "supervisor_brief": "Useful template.",
        }],
    }
    (reports / "2026-09-16.json").write_text(json.dumps(codex), encoding="utf-8")

    result = build_pages(reports, site)

    assert result["report_dates"] == ["2026-09-16", "2026-09-15"]
    assert (site / "reports" / "2026-09-15" / "index.html").exists()
    latest = (site / "latest" / "index.html").read_text(encoding="utf-8")
    assert "Codex canonical paper" in latest
    assert "72" in latest and "12" in latest


def test_page_contains_scores_evidence_notice_and_clickable_links(tmp_path: Path):
    reports = tmp_path / "reports"
    site = tmp_path / "site"
    _write_report(reports, "2026-09-16", evidence="ABSTRACT_ONLY")
    build_pages(reports, site)
    page = (site / "reports" / "2026-09-16" / "index.html").read_text(encoding="utf-8")

    assert "experimental similarity" in page
    assert "transferability" in page
    assert "idea value" in page
    assert "ABSTRACT_ONLY" in page
    assert "仅根据摘要分析，具体实验细节需查看原文" in page
    assert "FULLTEXT_READ" not in page
    assert "https://doi.org/10.1234/example" in page
    assert "https://pubmed.ncbi.nlm.nih.gov/123456/" in page
    assert "https://publisher.example/paper" in page
    assert "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/" in page


def test_fulltext_badge_and_review_notice_are_distinct(tmp_path: Path):
    reports = tmp_path / "reports"
    site = tmp_path / "site"
    _write_report(reports, "2026-09-16", evidence="FULLTEXT_READ")
    build_pages(reports, site)
    page = (site / "latest" / "index.html").read_text(encoding="utf-8")
    assert "FULLTEXT_READ" in page
    assert "已获取并进行全文证据审阅" in page
    assert "仅根据摘要分析，具体实验细节需查看原文" not in page


def test_html_escaping_and_raw_fulltext_are_not_published(tmp_path: Path):
    reports = tmp_path / "reports"
    site = tmp_path / "site"
    title = "<script>alert('xss')</script>"
    _write_report(reports, "2026-09-16", title=title, raw_fulltext="SECRET_RAW_FULLTEXT_DO_NOT_PUBLISH")
    build_pages(reports, site)
    rendered = "\n".join(file.read_text(encoding="utf-8") for file in site.rglob("*.html"))
    persisted = (site / "data" / "reports" / "2026-09-16.json").read_text(encoding="utf-8")
    assert title not in rendered
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in rendered
    assert "SECRET_RAW_FULLTEXT_DO_NOT_PUBLISH" not in rendered
    assert "SECRET_RAW_FULLTEXT_DO_NOT_PUBLISH" not in persisted


def test_project_pages_uses_relative_links_and_custom_domain_uses_settings_not_cname(tmp_path: Path):
    reports = tmp_path / "reports"
    site = tmp_path / "site"
    _write_report(reports, "2026-09-16")
    build_pages(reports, site, public_site_url="https://owner.github.io/pygl-research-radar")
    daily = (site / "reports" / "2026-09-16" / "index.html").read_text(encoding="utf-8")
    assert "../../assets/style.css" in daily
    assert 'href="/latest/"' not in daily
    assert 'href="../../latest/"' in daily
    assert 'href="../../archive/"' in daily

    custom_site = tmp_path / "custom-site"
    build_pages(reports, custom_site, public_site_url="https://radar.example.com")
    assert not (custom_site / "CNAME").exists()
    root = (custom_site / "index.html").read_text(encoding="utf-8")
    assert 'https://radar.example.com/latest/' in root
    assert "github.io" not in root


def test_public_site_url_resolution_supports_env_config_and_github_fallback():
    assert resolve_public_site_url(environ={"GITHUB_REPOSITORY": "owner/repo"}) == "https://owner.github.io/repo"
    assert public_report_url(environ={"PUBLIC_SITE_URL": "https://radar.example.com/"}) == "https://radar.example.com/latest/"
    assert resolve_public_site_url({"publishing": {"public_site_url": "https://config.example/radar"}}, environ={}) == "https://config.example/radar"


class _CaptureNotifier:
    def __init__(self):
        self.url = ""

    def send(self, message: str, *, url: str = "") -> NotificationResult:
        self.url = url
        return NotificationResult(True, "capture")


class _Publisher:
    def publish(self, markdown: str, *, report_date: str) -> PublicationResult:
        return PublicationResult(True, f"https://github.com/example/radar/issues/{report_date}", 9)


def test_wechat_receives_pages_latest_url(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PUBLIC_SITE_URL", "https://owner.github.io/pygl-research-radar")
    config = {
        "profile": {"keywords": ["PYGL"], "mechanisms": ["lysosome"], "experimental_patterns": ["rescue"]},
        "triage": {"batch_size": 20, "retain": 15}, "fulltext": {"enabled": False},
        "scoring": {"min_score": 40, "top_n": 5}, "output": {"directory": str(tmp_path / "reports")},
        "state": {"seen_cache": str(tmp_path / "seen.json"), "feedback": str(tmp_path / "feedback.json")},
    }
    notifier = _CaptureNotifier()
    result = run_radar(config, fixture_path=Path(__file__).parent.parent / "fixtures/sample_papers.json", dry_run=True, notifier=notifier, publisher=_Publisher())
    assert result.public_report_url == "https://owner.github.io/pygl-research-radar/latest/"
    assert notifier.url == result.public_report_url
    saved = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert saved["stats"]["pages_url"] == result.public_report_url
    assert saved["stats"]["issue_url"].startswith("https://github.com/")


def test_radar_can_defer_wechat_until_after_pages(tmp_path: Path):
    config = {
        "profile": {"keywords": ["PYGL"], "mechanisms": ["lysosome"], "experimental_patterns": ["rescue"]},
        "triage": {"batch_size": 20, "retain": 15}, "fulltext": {"enabled": False},
        "scoring": {"min_score": 40, "top_n": 5}, "output": {"directory": str(tmp_path / "reports")},
        "state": {"seen_cache": str(tmp_path / "seen.json"), "feedback": str(tmp_path / "feedback.json")},
    }
    notifier = _CaptureNotifier()
    result = run_radar(config, fixture_path=Path(__file__).parent.parent / "fixtures/sample_papers.json", dry_run=True, notifier=notifier, publisher=_Publisher(), notify=False)
    assert result.notification.ok and result.notification.provider == "deferred"
    assert notifier.url == ""
    assert json.loads(result.json_path.read_text(encoding="utf-8"))["stats"]["issue_url"].startswith("https://github.com/")


def test_notify_report_sends_the_selected_post_deploy_url(tmp_path: Path):
    reports = tmp_path / "reports"
    _write_report(reports, "2026-09-16")
    notifier = _CaptureNotifier()
    result = notify_report(reports / "2026-09-16.json", report_url="https://owner.github.io/pygl-research-radar/latest/", notifier=notifier)
    assert result.ok
    assert notifier.url == "https://owner.github.io/pygl-research-radar/latest/"


def test_pages_workflow_builds_on_pr_but_deploys_only_after_production_radar():
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "daily-radar.yml"
    text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert "pull_request:" in text
    assert "actions/deploy-pages@v4" in text
    assert workflow["jobs"]["pages"]["if"] == "github.event_name != 'pull_request' && needs.radar.result == 'success'"
    assert workflow["jobs"]["notify"]["if"] == "always() && needs.radar.result == 'success'"
    assert workflow["jobs"]["notify"]["needs"] == ["radar", "pages"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    radar_steps = workflow["jobs"]["radar"]["steps"]
    radar_run = next(step for step in radar_steps if "defer-notification" in str(step.get("run", "")))
    assert "--defer-notification" in radar_run["run"]
    assert not any("WECHAT_APP_ID" in str(step.get("env", {})) for step in radar_steps)
    assert any("pygl_radar.pages" in str(step.get("run", "")) for step in workflow["jobs"]["ci"]["steps"])
    assert "contents: write" in text and "pages: write" in text and "id-token: write" in text
