from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from .config import load_config, resolve_path
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
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "feedback":
        config = load_config(args.config)
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
    result = run_radar(args.config, dry_run=args.dry_run, fixture_path=args.fixture, disable_fulltext=args.no_fulltext, notify=not args.defer_notification)
    print(f"Recommended {len(result.papers)} papers; digest: {result.markdown_path}")
    if not result.notification.ok:
        parser.error(f"WeChat push failed: {result.notification.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
