from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from .config import load_config, resolve_path
from .codex_mode import (
    CodexModeError,
    hydrate_codex_workspace,
    prepare_codex_workspace,
    validate_publishable_codex_report,
    validate_codex_workspace,
)
from .codex_publishing import send_codex_report_notification
from .feedback import FeedbackState
from .pipeline import notify_report, run_radar


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PYGL Research Radar v1")
    parser.add_argument("--config", default=None, help="YAML configuration path")
    parser.add_argument("--dry-run", action="store_true", help="use mock WeChat notifier and do not update seen cache")
    parser.add_argument("--fixture", help="JSON fixture for offline/reproducible runs")
    parser.add_argument("--no-fulltext", action="store_true", help="skip lawful full-text acquisition")
    parser.add_argument("--defer-notification", action="store_true", help="produce/publish the report without sending WeChat yet")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    feedback = subparsers.add_parser("feedback", help="record soft paper feedback")
    feedback.add_argument("--paper-id", required=True)
    feedback.add_argument("--label", required=True, choices=["relevant", "idea", "method", "skip"])
    notify = subparsers.add_parser("notify", help="send an existing JSON report to WeChat")
    notify.add_argument("--report", required=True, help="JSON report path")
    notify.add_argument("--url", default="", help="public report URL; falls back to the JSON report URLs")
    prepare = subparsers.add_parser("codex-prepare", help="prepare a deterministic Codex candidate workspace")
    prepare.add_argument("--config", dest="config_override", default=argparse.SUPPRESS, help="YAML configuration path")
    prepare.add_argument("--fixture", dest="fixture_override", default=argparse.SUPPRESS, help="offline candidate fixture")
    hydrate = subparsers.add_parser("codex-hydrate", help="lawfully hydrate a Codex shortlist")
    hydrate.add_argument("--workspace", required=True)
    hydrate.add_argument("--shortlist", required=True)
    hydrate.add_argument("--config", dest="config_override", default=argparse.SUPPRESS, help="YAML configuration path")
    validate = subparsers.add_parser("codex-validate", help="validate and publish sanitized Codex output")
    validate.add_argument("--workspace", required=True)
    validate.add_argument("--output-root", default="codex-output")
    publish_check = subparsers.add_parser("codex-publish-check", help="deterministically validate a committed Codex report")
    publish_check.add_argument("--report", required=True)
    codex_notify = subparsers.add_parser("codex-notify", help="send a compact WeChat entry point for a validated Codex report")
    codex_notify.add_argument("--report", required=True)
    codex_notify.add_argument("--url", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config_path = getattr(args, "config_override", None) or args.config
    try:
        if args.command == "codex-prepare":
            result = prepare_codex_workspace(config_path, fixture_path=getattr(args, "fixture_override", None))
            print(f"Prepared {result['candidate_count']} candidates: {result['workspace']}")
            return 0
        if args.command == "codex-hydrate":
            result = hydrate_codex_workspace(args.workspace, args.shortlist, config_or_path=config_path)
            print(f"Hydrated {len(result['paper_ids'])} papers: {result['workspace'] / 'evidence'}")
            return 0
        if args.command == "codex-validate":
            result = validate_codex_workspace(args.workspace, output_root=args.output_root)
            print(f"Validated {result['papers']} papers: {result['json']}")
            return 0
        if args.command == "codex-publish-check":
            report = validate_publishable_codex_report(args.report)
            print(f"Publishable Codex report: {report['date']} ({len(report['papers'])} papers)")
            return 0
        if args.command == "codex-notify":
            result = send_codex_report_notification(args.report, report_url=args.url, dry_run=args.dry_run)
            if not result.ok:
                parser.error(f"WeChat push failed: {result.error}")
            print(f"Sent Codex report notification: {args.report}")
            return 0
    except (CodexModeError, FileNotFoundError, OSError) as exc:
        parser.error(str(exc))
    if args.command == "feedback":
        config = load_config(config_path)
        state = FeedbackState(resolve_path(config, "feedback", default="state/feedback.json"))
        state.record(args.paper_id, args.label, timestamp=datetime.now(timezone.utc).isoformat())
        state.save()
        print(f"Recorded {args.label} feedback for {args.paper_id}")
        return 0
    if args.command == "notify":
        result = notify_report(args.report, report_url=args.url, dry_run=args.dry_run)
        if not result.ok:
            parser.error(f"WeChat push failed: {result.error}")
        print(f"Sent report notification: {args.report}")
        return 0
    result = run_radar(config_path, dry_run=args.dry_run, fixture_path=args.fixture, disable_fulltext=args.no_fulltext, notify=not args.defer_notification)
    print(f"Recommended {len(result.papers)} papers; digest: {result.markdown_path}")
    if not result.notification.ok:
        parser.error(f"WeChat push failed: {result.notification.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
