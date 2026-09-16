"""Deterministic GitHub-side publishing helpers for validated Codex reports.

Codex Automation never invokes this module.  It is used only by the GitHub
Actions publishing workflow after a sanitized report has crossed the commit
boundary, where Actions Secrets provide the existing WeChat credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .codex_mode import validate_publishable_codex_report
from .notifiers import MockNotifier, Notifier, WeChatNotifier, WeChatNotifierConfig
from .notifiers.base import NotificationResult


def _short_text(value: Any, *, limit: int = 110) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "与当前 PYGL/efferocytosis 课题的实验逻辑具有可检验的映射。"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_codex_wechat_message(report: dict[str, Any], *, report_url: str) -> str:
    """Render a compact entry-point message; detailed review stays on Pages."""
    stats = report["stats"]
    lines = [
        f"PYGL Research Radar · {report['date']}",
        "",
        f"今日扫描 {stats['candidate_pool']} 篇",
        f"深读 {stats['shortlist']} 篇",
        f"最终推荐 {stats['recommended']} 篇",
        "",
    ]
    for index, paper in enumerate(report["papers"], 1):
        lines.extend([
            f"{index}. {_short_text(paper['title'], limit=150)}",
            f"   为什么值得看：{_short_text(paper.get('topic_mapping') or paper.get('supervisor_brief'))}",
            "",
        ])
    lines.extend(["完整科研拆解：", report_url])
    return "\n".join(lines)


def send_codex_report_notification(
    report_path: str | Path,
    *,
    report_url: str,
    notifier: Notifier | None = None,
    dry_run: bool = False,
) -> NotificationResult:
    """Validate, render, and send one short WeChat entry-point notification."""
    report = validate_publishable_codex_report(report_path)
    sender = notifier or (MockNotifier() if dry_run else WeChatNotifier(WeChatNotifierConfig.from_env()))
    return sender.send(render_codex_wechat_message(report, report_url=report_url), url=report_url)


__all__ = ["render_codex_wechat_message", "send_codex_report_notification"]
