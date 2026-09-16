"""Static GitHub Pages renderer for the daily radar digest.

The renderer deliberately consumes the already-reviewed JSON report.  It does
not call PubMed, an LLM, or a full-text source, and it never publishes the raw
full-text cache to the site.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .review import sanitize_abstract_claim

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCORE_FIELDS = (
    ("direct_relevance", "direct relevance"),
    ("mechanism_relevance", "mechanism relevance"),
    ("experimental_similarity", "experimental similarity"),
    ("transferability", "transferability"),
    ("idea_value", "idea value"),
    ("evidence_quality", "evidence quality"),
)
FOCUS_FIELDS = {"experimental_similarity", "transferability", "idea_value"}
REVIEW_FIELDS = (
    "core_finding",
    "why_recommended",
    "mechanism_mapping",
    "transferable_strategy",
    "new_hypothesis",
)

STYLE = """
:root { color-scheme: light; --ink: #172033; --muted: #667085; --line: #e4e7ec; --paper: #ffffff; --canvas: #f6f8fb; --blue: #2457d6; --blue-soft: #eaf0ff; --green: #087443; --green-soft: #e7f7ee; --amber: #8a4b08; --amber-soft: #fff4df; }
* { box-sizing: border-box; }
html { background: var(--canvas); }
body { margin: 0; color: var(--ink); background: var(--canvas); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; line-height: 1.55; overflow-x: hidden; }
a { color: var(--blue); text-decoration: none; }
a:hover, a:focus { text-decoration: underline; }
.shell { width: min(calc(100% - 28px), 860px); margin: 0 auto; padding: 20px 0 34px; }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.brand { color: var(--ink); font-size: 1.02rem; font-weight: 760; letter-spacing: .01em; }
.nav { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px; font-size: .86rem; }
.hero { padding: 22px; border: 1px solid #dfe6f6; border-radius: 20px; background: linear-gradient(145deg, #f0f4ff 0%, #fff 72%); box-shadow: 0 8px 24px rgba(27, 46, 94, .06); }
.eyebrow { margin: 0 0 5px; color: var(--blue); font-size: .75rem; font-weight: 750; letter-spacing: .1em; text-transform: uppercase; }
h1, h2, h3 { line-height: 1.25; }
h1 { margin: 0; font-size: clamp(1.65rem, 7vw, 2.55rem); letter-spacing: -.035em; }
h2 { margin: 0; font-size: 1.13rem; }
h3 { margin: 0; font-size: .96rem; }
.date { margin: 8px 0 0; color: var(--muted); font-size: .94rem; }
.metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-top: 18px; }
.metric { padding: 11px 12px; border: 1px solid rgba(36, 87, 214, .12); border-radius: 13px; background: rgba(255,255,255,.82); }
.metric-value { display: block; font-size: 1.3rem; font-weight: 780; letter-spacing: -.02em; }
.metric-label { display: block; color: var(--muted); font-size: .76rem; }
.section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin: 24px 0 10px; }
.section-heading p { margin: 0; color: var(--muted); font-size: .84rem; }
.card { margin: 12px 0; padding: 17px; border: 1px solid var(--line); border-radius: 17px; background: var(--paper); box-shadow: 0 4px 14px rgba(16, 24, 40, .045); }
.card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.card-title { min-width: 0; }
.card-title h2 { overflow-wrap: anywhere; }
.meta { margin: 7px 0 0; color: var(--muted); font-size: .83rem; overflow-wrap: anywhere; }
.final-score { flex: 0 0 auto; min-width: 62px; padding: 7px 8px; border-radius: 12px; color: #fff; background: var(--blue); text-align: center; }
.final-score strong { display: block; font-size: 1.13rem; line-height: 1; }
.final-score span { display: block; margin-top: 3px; font-size: .68rem; opacity: .86; }
.evidence-row { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin: 13px 0 3px; }
.badge, .chip { display: inline-flex; align-items: center; min-height: 25px; padding: 3px 8px; border-radius: 999px; font-size: .73rem; font-weight: 700; }
.badge.fulltext { color: var(--green); background: var(--green-soft); }
.badge.abstract { color: var(--amber); background: var(--amber-soft); }
.notice { width: 100%; padding: 8px 10px; border-left: 3px solid #e49b29; border-radius: 7px; color: #754108; background: #fff9ed; font-size: .82rem; }
.score-list { display: grid; gap: 9px; margin: 15px 0 16px; }
.score-item { min-width: 0; }
.score-label { display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: .75rem; }
.score-label b { color: var(--ink); font-weight: 650; overflow-wrap: anywhere; }
.score-track { height: 7px; margin-top: 4px; overflow: hidden; border-radius: 99px; background: #edf0f5; }
.score-fill { height: 100%; min-width: 2px; border-radius: inherit; background: #9aa5b5; }
.score-item.focus { padding: 6px 8px 7px; border-radius: 9px; background: #f1f5ff; }
.score-item.focus .score-label b { color: var(--blue); }
.score-item.focus .score-fill { background: var(--blue); }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.chip { color: #344054; background: #f2f4f7; font-weight: 600; }
.chip.focus { color: #1d4ed8; background: var(--blue-soft); }
.facts { display: grid; gap: 11px; margin-top: 12px; }
.fact { min-width: 0; }
.fact-label { display: block; margin-bottom: 2px; color: var(--muted); font-size: .76rem; font-weight: 700; }
.fact p { margin: 0; overflow-wrap: anywhere; font-size: .9rem; }
.links { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 16px; padding-top: 13px; border-top: 1px solid var(--line); font-size: .8rem; }
.links a { padding: 4px 8px; border: 1px solid #cbd5ef; border-radius: 8px; background: #fbfcff; }
.empty { margin-top: 14px; padding: 20px 17px; border: 1px dashed #c8ced9; border-radius: 15px; color: var(--muted); background: var(--paper); }
.archive-list { display: grid; gap: 10px; margin-top: 14px; }
.archive-item { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; padding: 15px; border: 1px solid var(--line); border-radius: 14px; background: var(--paper); }
.archive-date { color: var(--ink); font-weight: 760; }
.archive-count { color: var(--muted); font-size: .8rem; text-align: right; }
.archive-title { grid-column: 1 / -1; color: var(--muted); font-size: .87rem; overflow-wrap: anywhere; }
footer { margin-top: 27px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: .79rem; }
footer p { margin: 6px 0; overflow-wrap: anywhere; }
@media (min-width: 620px) { .shell { padding-top: 28px; } .metrics { grid-template-columns: repeat(5, minmax(0, 1fr)); } .facts { grid-template-columns: repeat(2, minmax(0, 1fr)); } .card { padding: 21px; } }
""".strip() + "\n"


def resolve_public_site_url(config: dict[str, Any] | None = None, *, environ: dict[str, str] | None = None) -> str:
    """Resolve the public base URL without hard-coding an owner or domain."""
    env = environ or os.environ
    configured = str(env.get("PUBLIC_SITE_URL", "")).strip()
    if not configured and config:
        configured = str(config.get("publishing", {}).get("public_site_url", "")).strip()
    if not configured:
        repository = str(env.get("GITHUB_REPOSITORY", "")).strip()
        owner, separator, name = repository.partition("/")
        if separator and owner and name:
            configured = f"https://{owner}.github.io/{name}"
    parsed = urlparse(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return configured.rstrip("/")


def public_report_url(config: dict[str, Any] | None = None, *, environ: dict[str, str] | None = None) -> str:
    base = resolve_public_site_url(config, environ=environ)
    return f"{base}/latest/" if base else ""


def _safe_url(value: Any) -> str | None:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _valid_date(value: Any) -> str | None:
    candidate = str(value or "").strip()
    return candidate if DATE_RE.fullmatch(candidate) else None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_text(value: Any, default: str = "未提供。") -> str:
    text = str(value or "").strip()
    return text or default


def _safe_paper(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields needed by the public digest; notably omit abstract/fulltext."""
    result: dict[str, Any] = {
        "title": _safe_text(data.get("title"), "未提供标题"),
        "journal": _safe_text(data.get("journal"), "期刊未提供"),
        "publication_date": _safe_text(data.get("publication_date"), "日期未提供"),
        "doi": str(data.get("doi") or "").strip(),
        "pmid": str(data.get("pmid") or "").strip(),
        "publisher_url": str(data.get("publisher_url") or "").strip(),
        "fulltext_source_url": str(data.get("fulltext_source_url") or "").strip(),
        "evidence_level": data.get("evidence_level") if data.get("evidence_level") in {"FULLTEXT_READ", "ABSTRACT_ONLY"} else "ABSTRACT_ONLY",
        "final_score": _number(data.get("final_score")),
        "scores": {},
        "review": {},
    }
    scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    result["scores"] = {key: max(0.0, min(100.0, _number(scores.get(key)))) for key, _ in SCORE_FIELDS}
    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    for field in REVIEW_FIELDS:
        value = _safe_text(review.get(field))
        if result["evidence_level"] == "ABSTRACT_ONLY":
            value, _ = sanitize_abstract_claim(value)
        result["review"][field] = value
    return result


def _safe_report(raw: dict[str, Any], date_hint: str) -> dict[str, Any] | None:
    report_date = _valid_date(raw.get("date")) or _valid_date(date_hint)
    if not report_date:
        return None
    stats_raw = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    stats: dict[str, Any] = {}
    for key in ("retrieved", "deduplicated", "triaged", "reviewed", "fulltext_reviewed", "recommended"):
        stats[key] = int(max(0.0, _number(stats_raw.get(key))))
    evidence = stats_raw.get("evidence") if isinstance(stats_raw.get("evidence"), dict) else {}
    stats["evidence"] = {key: int(max(0.0, _number(evidence.get(key)))) for key in ("FULLTEXT_READ", "ABSTRACT_ONLY")}
    for key in ("issue_url", "pages_url"):
        candidate = _safe_url(stats_raw.get(key))
        if candidate:
            stats[key] = candidate
    failures = stats_raw.get("source_failures")
    if isinstance(failures, dict):
        stats["source_failures"] = {str(key): _safe_text(value, "失败") for key, value in failures.items()}
    papers = raw.get("papers") if isinstance(raw.get("papers"), list) else []
    return {"version": 1, "date": report_date, "stats": stats, "papers": [_safe_paper(item) for item in papers if isinstance(item, dict)]}


def _read_reports(directories: Iterable[Path]) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for directory in directories:
        if not directory.exists():
            continue
        for report_file in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(report_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                report = _safe_report(raw, report_file.stem)
                if report:
                    reports[report["date"]] = report
    return reports


def _href(value: str) -> str:
    return html.escape(value, quote=True)


def _join_relative(prefix: str, suffix: str) -> str:
    if not suffix:
        return prefix or "./"
    return f"{prefix}{suffix}" if prefix else suffix


def _display_text(value: Any) -> str:
    return html.escape(_safe_text(value)).replace("\n", "<br>")


def _metric(stats: dict[str, Any], key: str, fallback: int = 0) -> int:
    return int(max(0, _number(stats.get(key), fallback)))


def _links(paper: dict[str, Any]) -> str:
    links: list[tuple[str, str]] = []
    doi = str(paper.get("doi") or "").strip()
    if doi:
        links.append(("DOI", f"https://doi.org/{doi}"))
    pmid = str(paper.get("pmid") or "").strip()
    if pmid:
        links.append(("PubMed", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"))
    links.extend((label, str(paper.get(key) or "").strip()) for label, key in (("Publisher", "publisher_url"), ("全文来源", "fulltext_source_url")))
    rendered = [f"<a href=\"{_href(url)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(label)}</a>" for label, raw in links if (url := _safe_url(raw))]
    return "".join(rendered) or "<span>无外部链接</span>"


def _daily_body(report: dict[str, Any], prefix: str) -> str:
    date_text = html.escape(str(report["date"]))
    stats = report["stats"]
    fulltext_reviewed = _metric(stats, "fulltext_reviewed", _metric(stats.get("evidence", {}), "FULLTEXT_READ"))
    metrics = (
        ("retrieved", "本日扫描"),
        ("deduplicated", "去重候选"),
        ("triaged", "AI triage"),
        ("fulltext_reviewed", "全文审阅"),
        ("recommended", "最终推荐"),
    )
    metric_html = "".join(f"<div class=\"metric\"><span class=\"metric-value\">{fulltext_reviewed if key == 'fulltext_reviewed' else _metric(stats, key, len(report['papers']) if key == 'recommended' else 0)}</span><span class=\"metric-label\">{label}</span></div>" for key, label in metrics)
    cards: list[str] = []
    for index, paper in enumerate(report["papers"], 1):
        evidence = paper["evidence_level"]
        is_fulltext = evidence == "FULLTEXT_READ"
        evidence_html = '<span class="badge fulltext">FULLTEXT_READ</span><span class="meta">已获取并进行全文证据审阅</span>' if is_fulltext else '<span class="badge abstract">ABSTRACT_ONLY</span><span class="meta">仅摘要证据</span>'
        score_items = []
        chips = []
        for key, label in SCORE_FIELDS:
            value = max(0.0, min(100.0, _number(paper["scores"].get(key))))
            focus = " focus" if key in FOCUS_FIELDS else ""
            score_items.append(f"<div class=\"score-item{focus}\"><div class=\"score-label\"><b>{html.escape(label)}</b><span>{value:.0f}</span></div><div class=\"score-track\"><div class=\"score-fill\" style=\"width:{value:.0f}%\"></div></div></div>")
            chips.append(f"<span class=\"chip{focus}\">{html.escape(label)} {value:.0f}</span>")
        facts = (
            ("一句话核心发现", paper["review"].get("core_finding")),
            ("为什么推荐给当前 PYGL 课题", paper["review"].get("why_recommended")),
            ("Mechanism mapping", paper["review"].get("mechanism_mapping")),
            ("最值得借鉴的实验套路", paper["review"].get("transferable_strategy")),
            ("Concrete wet-lab idea", paper["review"].get("new_hypothesis")),
        )
        fact_html = "".join(f"<div class=\"fact\"><span class=\"fact-label\">{html.escape(label)}</span><p>{_display_text(value)}</p></div>" for label, value in facts)
        cards.append(
            f"<article class=\"card\"><div class=\"card-head\"><div class=\"card-title\"><h2>{index}. {html.escape(paper['title'])}</h2><p class=\"meta\">{html.escape(paper['journal'])} · {html.escape(paper['publication_date'])}</p></div><div class=\"final-score\"><strong>{_number(paper['final_score']):.1f}</strong><span>final score</span></div></div>"
            f"<div class=\"evidence-row\">{evidence_html}</div>"
            f"{'<div class=\"notice\">仅根据摘要分析，具体实验细节需查看原文</div>' if not is_fulltext else ''}"
            f"<div class=\"chip-row\">{''.join(chips)}</div><div class=\"score-list\">{''.join(score_items)}</div><div class=\"facts\">{fact_html}</div><div class=\"links\">{_links(paper)}</div></article>"
        )
    if not cards:
        cards.append('<div class="empty">今日没有达到质量门槛的推荐；不为凑数输出论文。</div>')
    return f"<section class=\"hero\"><p class=\"eyebrow\">PYGL Research Radar</p><h1>科研日报</h1><p class=\"date\">{date_text}</p><div class=\"metrics\">{metric_html}</div></section><div class=\"section-heading\"><h2>今日推荐</h2><p>实验结构优先于关键词重合</p></div>{''.join(cards)}"


def _document(title: str, body: str, prefix: str, *, public_site_url: str, report_date: str | None = None, canonical_path: str | None = None, issue_url: str = "") -> str:
    canonical = ""
    if public_site_url:
        suffix = canonical_path or ("latest/" if report_date is None else f"reports/{report_date}/")
        canonical = f"<link rel=\"canonical\" href=\"{_href(public_site_url + '/' + suffix)}\">"
    home = _join_relative(prefix, "")
    latest = _join_relative(prefix, "latest/")
    archive = _join_relative(prefix, "archive/")
    footer_issue = f"<p><a href=\"{_href(issue_url)}\" target=\"_blank\" rel=\"noopener noreferrer\">打开对应 GitHub 日报 Issue</a>，评论：<code>feedback &lt;DOI或PMID&gt; method</code></p>" if issue_url else "<p>反馈格式：打开对应 GitHub 日报 Issue，评论：<code>feedback &lt;DOI或PMID&gt; method</code></p>"
    return f"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><meta name=\"description\" content=\"PYGL Research Radar 中文科研日报\"><title>{html.escape(title)}</title>{canonical}<link rel=\"stylesheet\" href=\"{_href(_join_relative(prefix, 'assets/style.css'))}\"></head><body><main class=\"shell\"><div class=\"topbar\"><a class=\"brand\" href=\"{_href(home)}\">PYGL Research Radar</a><nav class=\"nav\"><a href=\"{_href(latest)}\">最新一期</a><a href=\"{_href(archive)}\">历史日报</a></nav></div>{body}<footer><p>页面只展示 AI digest、证据状态与合法原文链接，不包含原始全文缓存。</p>{footer_issue}</footer></main></body></html>\n"


def _archive_document(reports: list[dict[str, Any]], prefix: str, public_site_url: str) -> str:
    entries = []
    for report in reports:
        date_text = html.escape(report["date"])
        papers = report["papers"]
        top_title = _safe_text(papers[0].get("title"), "无推荐") if papers else "无推荐"
        href = _join_relative(prefix, f"reports/{report['date']}/")
        entries.append(f"<a class=\"archive-item\" href=\"{_href(href)}\"><span class=\"archive-date\">{date_text}</span><span class=\"archive-count\">{len(papers)} 篇推荐</span><span class=\"archive-title\">Top paper：{html.escape(top_title)}</span></a>")
    body = f"<section class=\"hero\"><p class=\"eyebrow\">PYGL Research Radar</p><h1>历史日报</h1><p class=\"date\">按日期倒序，日报 URL 永久稳定</p></section><div class=\"section-heading\"><h2>Archive</h2><p>{len(reports)} 期</p></div><div class=\"archive-list\">{''.join(entries) or '<div class="empty">暂无历史日报。</div>'}</div>"
    return _document("PYGL Research Radar · Archive", body, prefix, public_site_url=public_site_url, canonical_path="archive/")


def build_pages(reports_dir: str | Path = "reports", output_dir: str | Path = "site", *, public_site_url: str = "", repository: str = "") -> dict[str, Any]:
    """Build root/latest/archive/permanent report URLs from JSON reports."""
    reports_path = Path(reports_dir)
    site_path = Path(output_dir)
    site_path.mkdir(parents=True, exist_ok=True)
    persistent_path = site_path / "data" / "reports"
    directories = [reports_path]
    if persistent_path.resolve() != reports_path.resolve():
        directories.append(persistent_path)
    reports_by_date = _read_reports(directories)
    reports = [reports_by_date[key] for key in sorted(reports_by_date, reverse=True)]
    persistent_path.mkdir(parents=True, exist_ok=True)
    for report in reports:
        (persistent_path / f"{report['date']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (site_path / "assets").mkdir(parents=True, exist_ok=True)
    (site_path / "assets" / "style.css").write_text(STYLE, encoding="utf-8")
    public_site_url = (_safe_url(public_site_url) or "").rstrip("/")
    latest = reports[0] if reports else {"date": "暂无日报", "stats": {}, "papers": []}
    for filename, prefix in ((site_path / "index.html", ""), (site_path / "latest" / "index.html", "../")):
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(_document(f"PYGL Research Radar · {latest['date']}", _daily_body(latest, prefix), prefix, public_site_url=public_site_url, canonical_path="latest/", issue_url=str(latest.get("stats", {}).get("issue_url", ""))), encoding="utf-8")
    archive_path = site_path / "archive" / "index.html"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(_archive_document(reports, "../", public_site_url), encoding="utf-8")
    for report in reports:
        report_path = site_path / "reports" / report["date"] / "index.html"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_document(f"PYGL Research Radar · {report['date']}", _daily_body(report, "../../"), "../../", public_site_url=public_site_url, report_date=report["date"], issue_url=str(report.get("stats", {}).get("issue_url", ""))), encoding="utf-8")
    (site_path / ".nojekyll").write_text("\n", encoding="utf-8")
    return {"output_dir": str(site_path), "report_dates": [report["date"] for report in reports], "latest_date": latest["date"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the static PYGL Research Radar Pages site")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--output", default="site")
    parser.add_argument("--public-site-url", default="")
    parser.add_argument("--repository", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    site_url = args.public_site_url.strip() or resolve_public_site_url(environ={**os.environ, "GITHUB_REPOSITORY": args.repository or os.environ.get("GITHUB_REPOSITORY", "")})
    result = build_pages(args.reports, args.output, public_site_url=site_url, repository=args.repository)
    print(f"Built Pages site: {result['output_dir']} ({len(result['report_dates'])} reports)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
