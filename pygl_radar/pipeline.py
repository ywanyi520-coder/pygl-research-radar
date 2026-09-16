from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import load_config, resolve_path
from .dedup import SeenCache, deduplicate_papers
from .digest import render_html, render_markdown, render_wechat_message
from .feedback import FeedbackState
from .fulltext import FulltextAcquirer, apply_fulltext
from .llm import client_from_env
from .models import Paper
from .notifiers import Notifier, WeChatNotifier, WeChatNotifierConfig, MockNotifier, NotificationResult
from .publishing import GitHubIssuePublisher, NoopPublisher, PublicationResult, ReportPublisher
from .review import review_paper
from .scoring import rank_papers, score_paper
from .sources import collect_candidates
from .sources.common import HTTPClient
from .triage import triage_batch

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    papers: list[Paper]
    stats: dict[str, Any]
    markdown_path: Path
    html_path: Path
    json_path: Path
    notification: NotificationResult
    report_url: str = ""


def _fixture(path: str | Path) -> list[Paper]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("papers", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("fixture must be a JSON list or an object with papers")
    return [Paper.from_dict(item) for item in items if isinstance(item, dict)]


def _output_dir(config: dict[str, Any]) -> Path:
    raw = Path(str(config.get("output", {}).get("directory", "reports"))).expanduser()
    config_path = config.get("_path")
    if not raw.is_absolute() and config_path:
        raw = Path(config_path).parent / raw
    return raw


def _within_window(paper: Paper, now: datetime, hours: int) -> bool:
    """Conservatively enforce the 48-hour freshness gate when a full date exists."""
    if not paper.publication_date:
        return True
    try:
        value = datetime.fromisoformat(str(paper.publication_date).replace("Z", "+00:00"))
    except ValueError:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return current - timedelta(hours=hours) <= value <= current


def _candidate_order(papers: list[Paper], config: dict[str, Any]) -> list[Paper]:
    preferred = {str(journal).casefold() for journal in config.get("sources", {}).get("prioritize_journals", [])}
    def sort_key(paper: Paper) -> tuple[int, str]:
        preferred_hit = int(any(name and name in paper.journal.casefold() for name in preferred))
        return preferred_hit, str(paper.publication_date or "")
    return sorted(papers, key=sort_key, reverse=True)


def _notifier(dry_run: bool, notifier: Notifier | None) -> Notifier:
    if notifier is not None:
        return notifier
    if dry_run:
        return MockNotifier()
    return WeChatNotifier(WeChatNotifierConfig.from_env())


def _publisher(config: dict[str, Any], dry_run: bool, publisher: ReportPublisher | None) -> ReportPublisher:
    if publisher is not None:
        return publisher
    if dry_run or not bool(config.get("publishing", {}).get("enabled", True)):
        return NoopPublisher()
    return GitHubIssuePublisher.from_env()


def _ingest_remote_feedback(feedback: FeedbackState) -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repository:
        return 0
    try:
        from .publishing import fetch_github_issue_comments
        total = 0
        for entry in feedback.data.get("report_issues", [])[-5:]:
            number = entry.get("number")
            if isinstance(number, int):
                comments = fetch_github_issue_comments(token, repository, number, api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
                total += feedback.ingest_comments(comments)
        return total
    except Exception as exc:
        logger.warning("Remote feedback ingestion failed: %s", type(exc).__name__)
        return 0


def run_radar(
    config_or_path: dict[str, Any] | str | Path | None = None,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    fixture_path: str | Path | None = None,
    disable_fulltext: bool = False,
    notifier: Notifier | None = None,
    publisher: ReportPublisher | None = None,
) -> RunResult:
    """Execute retrieval -> triage -> lawful full-text -> review -> digest -> push."""
    config = config_or_path if isinstance(config_or_path, dict) else load_config(config_or_path)
    now = now or datetime.now(timezone.utc)
    http = HTTPClient(timeout=float(config.get("fulltext", {}).get("timeout_seconds", 30)))
    if fixture_path:
        candidates = _fixture(fixture_path)
        source_stats: dict[str, Any] = {"retrieved": {"fixture": len(candidates)}, "failures": {}}
    else:
        candidates, source_stats = collect_candidates(config, now=now, http=http)
    window_hours = int(config.get("sources", {}).get("window_hours", 48))
    candidates = [paper for paper in candidates if _within_window(paper, now, window_hours)]
    unique = deduplicate_papers(_candidate_order(candidates, config))
    unique = unique[: int(config.get("sources", {}).get("max_candidates", 80))]
    seen = SeenCache(resolve_path(config, "seen_cache", default="state/seen.json"))
    unseen = [paper for paper in unique if not seen.contains(paper)]
    profile = config.get("profile", {})
    feedback = FeedbackState(resolve_path(config, "feedback", default="state/feedback.json"))
    feedback_ingested = _ingest_remote_feedback(feedback)
    triage_client = client_from_env("RADAR_TRIAGE_MODEL", timeout=90)
    triaged = triage_batch(
        unseen, profile, triage_client,
        batch_size=int(config.get("triage", {}).get("batch_size", 20)),
    )[: int(config.get("triage", {}).get("retain", 15))]
    fulltext_enabled = bool(config.get("fulltext", {}).get("enabled", True)) and not disable_fulltext
    acquirer = FulltextAcquirer(
        http=http,
        timeout=float(config.get("fulltext", {}).get("timeout_seconds", 30)),
        min_text_chars=int(config.get("fulltext", {}).get("min_text_chars", 200)),
    )
    review_client = client_from_env("RADAR_REVIEW_MODEL", timeout=120)
    reviewed = 0
    for paper in triaged:
        if fulltext_enabled:
            try:
                apply_fulltext(paper, acquirer, unpaywall_email=os.environ.get("UNPAYWALL_EMAIL", "").strip())
            except Exception as exc:
                logger.warning("Full-text acquisition failed for %s: %s", paper.title, exc)
                paper.evidence_level = "ABSTRACT_ONLY"
        review_paper(paper, review_client, profile)
        score_paper(paper, config.get("scoring", {}).get("weights"), feedback.modifier(paper))
        reviewed += 1
    scoring_config = config.get("scoring", {})
    recommended = rank_papers(
        triaged,
        top_n=int(scoring_config.get("top_n", 5)),
        min_score=float(scoring_config.get("min_score", 50)),
    )
    stats = {
        "retrieved": len(candidates),
        "deduplicated": len(unique),
        "unseen": len(unseen),
        "triaged": len(triaged),
        "reviewed": reviewed,
        "feedback_ingested": feedback_ingested,
        "recommended": len(recommended),
        "source_retrieved": source_stats.get("retrieved", {}),
        "source_failures": source_stats.get("failures", {}),
        "evidence": {
            "FULLTEXT_READ": sum(paper.evidence_level == "FULLTEXT_READ" for paper in triaged),
            "ABSTRACT_ONLY": sum(paper.evidence_level == "ABSTRACT_ONLY" for paper in triaged),
        },
    }
    report_date = now.date().isoformat()
    directory = _output_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path = directory / f"{report_date}.md"
    html_path = directory / f"{report_date}.html"
    json_path = directory / f"{report_date}.json"
    markdown_path.write_text(render_markdown(recommended, stats, report_date=report_date), encoding="utf-8")
    html_path.write_text(render_html(recommended, stats, report_date=report_date), encoding="utf-8")
    json_path.write_text(json.dumps({"version": 1, "date": report_date, "stats": stats, "papers": [paper.to_dict() for paper in recommended]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    feedback.remember_papers(recommended)
    try:
        publication = _publisher(config, dry_run, publisher).publish(markdown_path.read_text(encoding="utf-8"), report_date=report_date)
    except Exception as exc:
        logger.warning("Report publication setup failed: %s", type(exc).__name__)
        publication = PublicationResult(False, error="report publication setup failed")
    report_url = publication.url if publication.ok else ""
    if publication.ok and publication.issue_number is not None:
        feedback.record_report_issue(report_date, publication.issue_number, publication.url)
        message = render_wechat_message(recommended, stats, report_url=report_url)
        push_result = _notifier(dry_run, notifier).send(message, url=report_url)
    elif publication.ok:
        message = render_wechat_message(recommended, stats, report_url=report_url)
        push_result = _notifier(dry_run, notifier).send(message, url=report_url)
    else:
        push_result = NotificationResult(False, "report-publication", error=publication.error or "report publication failed")
    feedback.save()
    if push_result.ok and not dry_run:
        seen.mark(recommended)
        seen.save()
    return RunResult(recommended, stats, markdown_path, html_path, json_path, push_result, report_url)
