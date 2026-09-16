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


def _deep(paper: Paper) -> dict[str, Any]:
    value = (paper.review or {}).get("deep_dive")
    return value if isinstance(value, dict) else {}


def _deep_text(paper: Paper, field: str) -> str:
    value = str(_deep(paper).get(field, "") or "").strip()
    if paper.evidence_level == "ABSTRACT_ONLY":
        value, _ = sanitize_abstract_claim(value)
    return value or "NOT_EVALUABLE"


def _as_text(item: Any) -> str:
    if isinstance(item, dict):
        return "；".join(f"{key}: {value}" for key, value in item.items() if str(value or "").strip())
    return str(item or "").strip()


def _deep_list(paper: Paper, field: str) -> list[str]:
    raw = _deep(paper).get(field)
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        value = _as_text(item)
        if not value:
            continue
        if paper.evidence_level == "ABSTRACT_ONLY":
            value, _ = sanitize_abstract_claim(value)
        if value:
            values.append(value)
    return values


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


def _safe_href(value: str) -> str | None:
    return value if value.startswith(("https://", "http://")) else None


def _html_links(paper: Paper) -> str:
    values = []
    if paper.doi:
        values.append(("DOI", f"https://doi.org/{paper.doi}"))
    if paper.pmid:
        values.append(("PubMed", f"https://pubmed.ncbi.nlm.nih.gov/{paper.pmid}/"))
    if paper.publisher_url:
        values.append(("Publisher", paper.publisher_url))
    if paper.fulltext_source_url:
        values.append(("全文来源", paper.fulltext_source_url))
    links = [f"<a href='{html.escape(url, quote=True)}'>{html.escape(label)}</a>" for label, url in values if _safe_href(url)]
    return " · ".join(dict.fromkeys(links)) or "无外部链接"


def _markdown_list(title: str, items: list[str]) -> list[str]:
    if not items:
        return [f"### {title}", "- NOT_EVALUABLE", ""]
    return [f"### {title}", *[f"- {item}" for item in items], ""]


def _markdown_figure_walkthrough(paper: Paper) -> list[str]:
    raw = _deep(paper).get("figure_walkthrough")
    lines = ["### 逐图逻辑"]
    if not isinstance(raw, list) or not raw:
        lines += [f"- {_deep_text(paper, 'figure_limitations')}", ""]
        return lines
    for item in raw:
        if not isinstance(item, dict):
            continue
        figure = str(item.get("figure") or "Figure").strip()
        lines += [
            f"#### {figure}",
            f"- **要回答的问题：** {item.get('question') or 'NOT_EVALUABLE'}",
            f"- **实验/分析：** {item.get('approach') or 'NOT_EVALUABLE'}",
            f"- **关键结果：** {item.get('key_result') or 'NOT_EVALUABLE'}",
            f"- **在全文逻辑中的作用：** {item.get('logic_role') or 'NOT_EVALUABLE'}",
            f"- **限制：** {item.get('caveat') or 'NOT_EVALUABLE'}",
            f"- **证据基于：** {item.get('evidence_basis') or 'TEXT_OR_LEGEND_ONLY'}",
            "",
        ]
    lines += [f"> 图像证据说明：{_deep_text(paper, 'figure_limitations')}", ""]
    return lines


def render_markdown(papers: Iterable[Paper], stats: dict[str, Any], *, report_date: str | None = None) -> str:
    date_text = report_date or datetime.now().date().isoformat()
    selected = list(papers)
    lines = [
        f"# PYGL Research Radar — {date_text}", "",
        f"> 扫描 {stats.get('retrieved', 0)} 篇，去重后 {stats.get('deduplicated', 0)} 篇，深审 {stats.get('reviewed', 0)} 篇，journal-club 级拆解 {stats.get('deep_reviewed', 0)} 篇；最终展示 {len(selected)} 篇。", "",
        "> 反馈格式：在本报告 Issue 评论 `feedback <DOI或PMID> <relevant|idea|method|skip>`。", "",
    ]
    if stats.get("source_failures"):
        lines += ["> ⚠️ 部分来源失败：" + "; ".join(f"{key}: {value}" for key, value in stats["source_failures"].items()), ""]
    if not selected:
        lines.append("今日没有达到质量门槛的推荐；不为凑数输出论文。\n")
        return "\n".join(lines)
    for index, paper in enumerate(selected, 1):
        deep = _deep(paper)
        lines += [
            f"## {index}. {paper.title}",
            f"- citation / journal / date: {', '.join(paper.authors[:3]) or '作者未提供'}; {paper.journal or '期刊未提供'}; {paper.publication_date or '日期未提供'}",
            f"- final score: **{paper.final_score:.2f}** | sub-scores: " + ", ".join(f"{key}={value:.1f}" for key, value in paper.scores.items()),
            f"- evidence: **{paper.evidence_level}**",
            f"- figure evidence mode: **{deep.get('figure_evidence_mode', 'NOT_EVALUABLE')}**",
            f"- links: {_links(paper)}", "",
            "### 快速结论",
            f"- **一句话核心发现：** {_text(paper, 'core_finding')}",
            f"- **为什么推荐：** {_text(paper, 'why_recommended')}",
            f"- **机制映射：** {_text(paper, 'mechanism_mapping')}",
            f"- **最值得搬的实验套路：** {_text(paper, 'transferable_strategy')}",
            f"- **直接给当前课题的假设：** {_text(paper, 'new_hypothesis')}", "",
            "### 背景与科学问题",
            f"- **领域背景：** {_deep_text(paper, 'background')}",
            f"- **知识缺口：** {_deep_text(paper, 'knowledge_gap')}",
            f"- **科学问题：** {_deep_text(paper, 'scientific_question')}",
            f"- **中心假设：** {_deep_text(paper, 'central_hypothesis')}", "",
            "### 整体实验思路",
            _deep_text(paper, "study_design"), "",
            "### 作者真正支持的因果链",
            _deep_text(paper, "causal_chain"), "",
        ]
        lines += _markdown_list("创新点", _deep_list(paper, "innovations"))
        lines += _markdown_figure_walkthrough(paper)
        lines += _markdown_list("关键 controls / blockade / activation / rescue / epistasis", _deep_list(paper, "key_controls_and_rescues"))
        lines += _markdown_list("证据链强项", _deep_list(paper, "strengths"))
        lines += _markdown_list("局限和潜在过度解释", _deep_list(paper, "limitations"))
        lines += ["### 对当前 PYGL 课题的具体映射", _deep_text(paper, "topic_mapping"), ""]
        lines += _markdown_list("可以直接借鉴的实验设计", _deep_list(paper, "actionable_ideas"))
        lines += _markdown_list("这篇文章不能替当前课题证明什么", _deep_list(paper, "do_not_overclaim"))
        lines += ["### 给老师讲的一段话", _deep_text(paper, "supervisor_brief"), "", "---", ""]
    return "\n".join(lines)


def _html_list(items: list[str]) -> str:
    if not items:
        return "<p>NOT_EVALUABLE</p>"
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"


def render_html(papers: Iterable[Paper], stats: dict[str, Any], *, report_date: str | None = None) -> str:
    date_text = report_date or datetime.now().date().isoformat()
    selected = list(papers)
    blocks = [
        f"<h1>PYGL Research Radar — {html.escape(date_text)}</h1>",
        f"<p>扫描 {stats.get('retrieved', 0)} 篇，深审 {stats.get('reviewed', 0)} 篇，详细拆解 {stats.get('deep_reviewed', 0)} 篇；推荐 {len(selected)} 篇。</p>",
    ]
    if not selected:
        blocks.append("<p>今日没有达到质量门槛的推荐；不为凑数输出论文。</p>")
    for index, paper in enumerate(selected, 1):
        deep = _deep(paper)
        blocks.append(
            "<article>"
            f"<h2>{index}. {html.escape(paper.title)}</h2>"
            f"<p>{html.escape(paper.journal or '期刊未提供')} · {html.escape(paper.publication_date or '日期未提供')} · evidence: <strong>{html.escape(paper.evidence_level)}</strong></p>"
            f"<p><strong>Final score:</strong> {paper.final_score:.2f}</p>"
            f"<h3>核心发现</h3><p>{html.escape(_text(paper, 'core_finding'))}</p>"
            f"<h3>为什么推荐</h3><p>{html.escape(_text(paper, 'why_recommended'))}</p>"
            f"<h3>背景</h3><p>{html.escape(_deep_text(paper, 'background'))}</p>"
            f"<h3>知识缺口</h3><p>{html.escape(_deep_text(paper, 'knowledge_gap'))}</p>"
            f"<h3>整体实验思路</h3><p>{html.escape(_deep_text(paper, 'study_design'))}</p>"
            f"<h3>创新点</h3>{_html_list(_deep_list(paper, 'innovations'))}"
            f"<h3>关键 controls / rescue</h3>{_html_list(_deep_list(paper, 'key_controls_and_rescues'))}"
            f"<h3>因果链</h3><p>{html.escape(_deep_text(paper, 'causal_chain'))}</p>"
            f"<h3>对 PYGL 课题的映射</h3><p>{html.escape(_deep_text(paper, 'topic_mapping'))}</p>"
            f"<h3>可以直接借鉴的实验</h3>{_html_list(_deep_list(paper, 'actionable_ideas'))}"
            f"<h3>不能过度解释的地方</h3>{_html_list(_deep_list(paper, 'do_not_overclaim'))}"
            f"<h3>给老师讲</h3><p>{html.escape(_deep_text(paper, 'supervisor_brief'))}</p>"
            f"<p>Links: {_html_links(paper)}</p>"
            "</article><hr>"
        )
    return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>PYGL Research Radar</title><body style='font-family:system-ui;line-height:1.65;max-width:980px;margin:30px auto;padding:0 20px'>" + "\n".join(blocks) + "</body></html>\n"


def render_wechat_message(papers: Iterable[Paper], stats: dict[str, Any], *, report_url: str = "") -> str:
    selected = list(papers)
    lines = [f"PYGL Research Radar｜扫描 {stats.get('retrieved', 0)} 篇，详细拆解 {stats.get('deep_reviewed', 0)} 篇，推荐 {len(selected)} 篇"]
    for index, paper in enumerate(selected[:5], 1):
        reason = _text(paper, "why_recommended").replace("\n", " ")
        lines.append(f"{index}. {paper.title[:90]}｜{paper.final_score:.1f}分｜{reason[:100]}")
    if not selected:
        lines.append("今日没有达到质量门槛的推荐。")
    if report_url:
        lines.append(f"完整报告：{report_url}")
    return "\n".join(lines)
