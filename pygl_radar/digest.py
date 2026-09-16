from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Iterable

from .models import Paper
from .review import sanitize_abstract_claim


def _text(paper: Paper, field: str) -> str:
    value = str((paper.review or {}).get(field, "") or "").strip()
    if paper.evidence_level == "ABSTRACT_ONLY":
        value, _ = sanitize_abstract_claim(value)
    return value or "未提供。"


def _links(paper: Paper) -> str:
    links = []
    if paper.doi:
        links.append(f"[DOI](https://doi.org/{paper.doi})")
    if paper.pmid:
        links.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{paper.pmid}/)")
    if paper.publisher_url:
        links.append(f"[Publisher]({paper.publisher_url})")
    if paper.fulltext_source_url:
        links.append(f"[全文来源]({paper.fulltext_source_url})")
    return " · ".join(dict.fromkeys(links)) or "无外部链接"


def render_markdown(papers: Iterable[Paper], stats: dict[str, Any], *, report_date: str | None = None) -> str:
    date_text = report_date or datetime.now().date().isoformat()
    selected = list(papers)
    lines = [
        f"# PYGL Research Radar v1 — {date_text}", "",
        f"> 扫描 {stats.get('retrieved', 0)} 篇，去重后 {stats.get('deduplicated', 0)} 篇，深审 {stats.get('reviewed', 0)} 篇；仅展示达到质量门槛的 {len(selected)} 篇。", "",
    ]
    if stats.get("source_failures"):
        lines += ["> ⚠️ 部分来源失败：" + "; ".join(f"{key}: {value}" for key, value in stats["source_failures"].items()), ""]
    if not selected:
        lines.append("今日没有达到质量门槛的推荐；不为凑数输出论文。\n")
        return "\n".join(lines)
    for index, paper in enumerate(selected, 1):
        lines += [
            f"## {index}. {paper.title}",
            f"- citation / journal / date: {', '.join(paper.authors[:3]) or '作者未提供'}; {paper.journal or '期刊未提供'}; {paper.publication_date or '日期未提供'}",
            f"- final score: **{paper.final_score:.2f}** | sub-scores: " + ", ".join(f"{key}={value:.1f}" for key, value in paper.scores.items()),
            f"- core finding: {_text(paper, 'core_finding')}",
            f"- why recommended: {_text(paper, 'why_recommended')}",
            f"- mechanism mapping: {_text(paper, 'mechanism_mapping')}",
            f"- transferable experimental strategy: {_text(paper, 'transferable_strategy')}",
            f"- new hypothesis / wet-lab idea: {_text(paper, 'new_hypothesis')}",
            f"- evidence: **{paper.evidence_level}**",
            "- feedback labels: `relevant` · `idea` · `method` · `skip`",
            f"- links: {_links(paper)}", "",
        ]
    return "\n".join(lines)


def render_html(papers: Iterable[Paper], stats: dict[str, Any], *, report_date: str | None = None) -> str:
    markdown = render_markdown(papers, stats, report_date=report_date)
    body = "<br>\n".join(html.escape(line) for line in markdown.splitlines())
    return f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>PYGL Research Radar</title><body><pre style='white-space:pre-wrap;font-family:system-ui'>{body}</pre></body></html>\n"


def render_wechat_message(papers: Iterable[Paper], stats: dict[str, Any], *, report_url: str = "") -> str:
    selected = list(papers)
    lines = [f"PYGL Research Radar｜扫描 {stats.get('retrieved', 0)} 篇，推荐 {len(selected)} 篇"]
    for index, paper in enumerate(selected[:5], 1):
        reason = _text(paper, "why_recommended").replace("\n", " ")
        lines.append(f"{index}. {paper.title[:90]}｜{paper.final_score:.1f}分｜{reason[:100]}")
    if not selected:
        lines.append("今日没有达到质量门槛的推荐。")
    if report_url:
        lines.append(f"完整报告：{report_url}")
    return "\n".join(lines)
