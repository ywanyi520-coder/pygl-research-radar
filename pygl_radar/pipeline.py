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
from .pages import public_report_url
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
    public_report_url: str = ""


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


def _preferred_journal(paper: Paper, config: dict[str, Any]) -> str:
    preferred = sorted(
        (str(journal).casefold() for journal in config.get("sources", {}).get("prioritize_journals", []) if str(journal).strip()),
        key=len,
        reverse=True,
    )
    journal = paper.journal.casefold()
    return next((name for name in preferred if name in journal), "")


def _is_journal_lane(paper: Paper) -> bool:
    return any(str(source).endswith("-journal") for source in paper.sources)


def _candidate_order(papers: list[Paper], config: dict[str, Any]) -> list[Paper]:
    def sort_key(paper: Paper) -> tuple[int, str]:
        preferred_hit = int(bool(_preferred_journal(paper, config)))
        return preferred_hit, str(paper.publication_date or "")
    return sorted(papers, key=sort_key, reverse=True)


def _stratified_cap(papers: list[Paper], config: dict[str, Any]) -> list[Paper]:
    """Reserve candidate capacity for both discovery lanes after deduplication."""
    source_settings = config.get("sources", {})
    max_candidates = max(0, int(source_settings.get("max_candidates", 80)))
    if len(papers) <= max_candidates:
        return papers
    journal_slot_cap = min(max_candidates, max(0, int(source_settings.get("journal_candidate_slots", max_candidates // 2))))
    topic_slot_cap = max_candidates - journal_slot_cap
    journal_quota = max(1, int(source_settings.get("journal_per_venue_quota", max(1, journal_slot_cap // 5))))
    journals = [paper for paper in papers if _is_journal_lane(paper)]
    topics = [paper for paper in papers if not _is_journal_lane(paper)]
    selected_journals: list[Paper] = []
    venue_counts: dict[str, int] = {}
    for paper in journals:
        venue = _preferred_journal(paper, config) or paper.journal.casefold() or "unknown"
        if venue_counts.get(venue, 0) >= journal_quota:
            continue
        selected_journals.append(paper)
        venue_counts[venue] = venue_counts.get(venue, 0) + 1
        if len(selected_journals) >= journal_slot_cap:
            break
    selected_topics = topics[:topic_slot_cap]
    selected = selected_journals + selected_topics
    # Backfill an unused reserved lane only from the opposite lane. Do not
    # relax the per-venue quota merely because one venue is high-volume.
    remaining = max_candidates - len(selected)
    if remaining and len(selected_journals) < journal_slot_cap:
        extra_topics = topics[topic_slot_cap : topic_slot_cap + remaining]
        selected.extend(extra_topics)
        remaining -= len(extra_topics)
    if remaining and len(selected_topics) < topic_slot_cap:
        selected_ids = {id(paper) for paper in selected_journals}
        for paper in journals:
            venue = _preferred_journal(paper, config) or paper.journal.casefold() or "unknown"
            if id(paper) in selected_ids or venue_counts.get(venue, 0) >= journal_quota:
                continue
            selected.append(paper)
            selected_ids.add(id(paper))
            venue_counts[venue] = venue_counts.get(venue, 0) + 1
            remaining -= 1
            if not remaining:
                break
    return selected[:max_candidates]


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
    notify: bool = True,
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
    unique = _stratified_cap(unique, config)
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
        min_html_text_chars=int(config.get("fulltext", {}).get("min_html_text_chars", 2000)),
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
        "fulltext_reviewed": sum(paper.evidence_level == "FULLTEXT_READ" for paper in triaged),
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
    def write_json_report() -> None:
        # Paper.to_dict() intentionally excludes the in-memory full-text cache.
        json_path.write_text(json.dumps({"version": 1, "date": report_date, "stats": stats, "papers": [paper.to_dict() for paper in recommended]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    write_json_report()
    feedback.remember_papers(recommended)
    try:
        publication = _publisher(config, dry_run, publisher).publish(markdown_path.read_text(encoding="utf-8"), report_date=report_date)
    except Exception as exc:
        logger.warning("Report publication setup failed: %s", type(exc).__name__)
        publication = PublicationResult(False, error="report publication setup failed")
    report_url = publication.url if publication.ok else ""
    pages_url = public_report_url(config)
    notification_url = pages_url or report_url
    if publication.ok and publication.issue_number is not None:
        feedback.record_report_issue(report_date, publication.issue_number, publication.url)
        stats["issue_url"] = report_url
    if publication.ok:
        stats["pages_url"] = pages_url
        write_json_report()
    if not publication.ok:
        push_result = NotificationResult(False, "report-publication", error=publication.error or "report publication failed")
    elif not notify:
        # Production Actions sends after Pages has deployed. Mark the radar
        # phase successful here so a later WeChat failure cannot replay papers.
        push_result = NotificationResult(True, "deferred")
    else:
        message = render_wechat_message(recommended, stats, report_url=notification_url)
        push_result = _notifier(dry_run, notifier).send(message, url=notification_url)
    feedback.save()
    if push_result.ok and not dry_run:
        seen.mark(recommended)
        seen.save()
    return RunResult(recommended, stats, markdown_path, html_path, json_path, push_result, report_url, pages_url)


def notify_report(
    report_path: str | Path,
    *,
    report_url: str = "",
    dry_run: bool = False,
    notifier: Notifier | None = None,
) -> NotificationResult:
    """Send an already-produced JSON digest without rerunning radar."""
    raw = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("report JSON must be an object")
    raw_papers = raw.get("papers") if isinstance(raw.get("papers"), list) else []
    papers = [Paper.from_dict(item) for item in raw_papers if isinstance(item, dict)]
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    selected_url = report_url.strip() or str(stats.get("pages_url") or stats.get("issue_url") or "").strip()
    message = render_wechat_message(papers, stats, report_url=selected_url)
    return _notifier(dry_run, notifier).send(message, url=selected_url)
