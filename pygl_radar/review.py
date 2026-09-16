from __future__ import annotations

import json
import logging
import re
from typing import Any

from .llm import LLMClient, parse_json_response
from .models import Paper

logger = logging.getLogger(__name__)

GUARDRAIL_PATTERNS = (
    re.compile(r"\bfig(?:ure)?\.?\s*[0-9]+(?:\s*[-–]\s*[0-9]+)?", re.I),
    re.compile(r"\bsample\s*size\b", re.I),
    re.compile(r"\bn\s*=\s*[0-9]+\b", re.I),
    re.compile(r"\b(?:dose|dosing)\s*[:=]?\s*[0-9]+", re.I),
    re.compile(r"\b[0-9]+(?:\.[0-9]+)?\s*(?:mg|μg|µg|ug|ng|nM|μM|µM|uM|mM|%)\b", re.I),
)
REVIEW_FIELDS = (
    "direct_relevance", "mechanism_relevance", "experimental_similarity",
    "transferability", "idea_value", "evidence_quality",
    "core_finding", "why_recommended", "mechanism_mapping",
    "transferable_strategy", "new_hypothesis",
)
DEEP_DIVE_TEXT_FIELDS = (
    "background",
    "knowledge_gap",
    "scientific_question",
    "central_hypothesis",
    "study_design",
    "causal_chain",
    "topic_mapping",
    "supervisor_brief",
    "figure_limitations",
)
DEEP_DIVE_LIST_FIELDS = (
    "innovations",
    "key_controls_and_rescues",
    "strengths",
    "limitations",
    "actionable_ideas",
    "do_not_overclaim",
    "figure_walkthrough",
)


def sanitize_abstract_claim(text: str) -> tuple[str, list[str]]:
    """Remove unsupported figure/dose/sample-size claims from abstract-only prose."""
    warnings: list[str] = []
    sentences = re.split(r"(?<=[.!?。！？])\s+", str(text or "").strip())
    kept: list[str] = []
    for sentence in sentences:
        matched = [pattern.pattern for pattern in GUARDRAIL_PATTERNS if pattern.search(sentence)]
        if matched:
            warnings.append("removed_unsupported_detail")
            continue
        kept.append(sentence)
    cleaned = " ".join(kept).strip()
    for pattern in GUARDRAIL_PATTERNS:
        if pattern.search(cleaned):
            warnings.append("removed_unsupported_detail")
            cleaned = pattern.sub("[摘要级细节已省略]", cleaned)
    return cleaned, list(dict.fromkeys(warnings))


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return default


def _default_review(paper: Paper) -> dict[str, Any]:
    text = (paper.fulltext_text if paper.evidence_level == "FULLTEXT_READ" else paper.abstract).strip()
    first_sentence = re.split(r"(?<=[.!?。！？])\s+", text)[0] if text else "摘要未提供足够结果信息。"
    triage = paper.triage or {}
    mechanism = "、".join(str(value) for value in (triage.get("matched_mechanisms") or [])[:4])
    return {
        "direct_relevance": triage.get("direct_relevance", 0),
        "mechanism_relevance": triage.get("mechanism_relevance", 0),
        "experimental_similarity": triage.get("experimental_similarity", 0),
        "transferability": triage.get("transferability", 0),
        "idea_value": triage.get("idea_value", 0),
        "evidence_quality": 70 if paper.evidence_level == "FULLTEXT_READ" else triage.get("evidence_quality", 30),
        "core_finding": first_sentence,
        "why_recommended": "实验逻辑与 PYGL 研究画像存在可迁移交集。" if triage.get("experimental_similarity", 0) else "与研究画像存在机制线索，建议人工核验。",
        "mechanism_mapping": mechanism or "需要结合原文实验链进一步映射。",
        "transferable_strategy": "优先复核其干预—表型—救援或下游旁路设计；具体复现条件需以原文为准。",
        "new_hypothesis": "可将该文的因果干预结构作为 PYGL—吞噬后处理表型的候选验证路径。",
        "model": "deterministic-fallback",
    }


def _section_chunks(text: str, *, chunk_chars: int = 4500, max_chunks: int = 8) -> str:
    """Build a review evidence map that samples the whole article, not only its front."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    positions: list[tuple[int, str]] = []
    for match in re.finditer(r"(?i)\b(abstract|introduction|materials and methods|methods|results|discussion|conclusions?|limitations|figure\s*\d+|fig\.?\s*\d+)\b", normalized):
        label = match.group(1).casefold()
        if not positions or match.start() - positions[-1][0] > 120:
            positions.append((match.start(), label))
    sections: list[tuple[str, str]] = []
    for index, (start, label) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(normalized)
        value = normalized[start:end].strip()
        if len(value) >= 180:
            sections.append((label, value[:chunk_chars]))
    if len(sections) >= 3:
        return "\n\n".join(f"[{label}] {value}" for label, value in sections[:max_chunks])
    chunks = [normalized[index : index + chunk_chars] for index in range(0, len(normalized), chunk_chars)]
    if len(chunks) <= max_chunks:
        selected = chunks
    else:
        indexes = sorted({round(index * (len(chunks) - 1) / (max_chunks - 1)) for index in range(max_chunks)})
        selected = [chunks[index] for index in indexes]
    return "\n\n".join(f"[fulltext chunk {index + 1}] {value}" for index, value in enumerate(selected))


def _prompt(paper: Paper, profile: dict[str, Any] | None = None) -> str:
    evidence = _section_chunks(paper.fulltext_text) if paper.evidence_level == "FULLTEXT_READ" else paper.abstract
    profile = profile or {}
    return (
        "你是严格的生物医学深度审稿器。只根据下列证据输出 JSON，不得编造未出现的细节。\n"
        f"evidence_level={paper.evidence_level}\n"
        "研究画像（本次运行冻结）：" + json.dumps(profile, ensure_ascii=False, sort_keys=True) + "\n"
        "必须以研究画像中的机制和实验模式解释相关性；陌生生物学只有在实验逻辑可迁移时才推荐。\n"
        "ABSTRACT_ONLY 只允许摘要层面的结论；禁止 figure/dose/sample-size/详细方法等全文级断言。"
        "FULLTEXT_READ 才能引用解析全文中实际出现的图号或方法。\n"
        "字段：direct_relevance, mechanism_relevance, experimental_similarity, transferability, idea_value, evidence_quality（0-100），"
        "core_finding, why_recommended, mechanism_mapping, transferable_strategy, new_hypothesis。只输出 JSON。\n"
        f"题名：{paper.title}\n期刊：{paper.journal}\n证据：{evidence}"
    )


def review_paper(paper: Paper, client: LLMClient, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    review = _default_review(paper)
    try:
        response = client.generate(
            _prompt(paper, profile),
            system="Evidence-level guardrail is mandatory. Return JSON only and never upgrade ABSTRACT_ONLY to full-text evidence.",
            max_tokens=3000,
        )
        parsed = parse_json_response(response)
        if isinstance(parsed, dict):
            for field in REVIEW_FIELDS:
                if field in parsed:
                    review[field] = parsed[field]
            review["model"] = getattr(client, "model", "configured-llm")
    except Exception as exc:
        logger.warning("Deep review failed for %s; using fallback: %s", paper.title, exc)
    for field in REVIEW_FIELDS[:6]:
        review[field] = _clamp(review.get(field), _clamp(paper.triage.get(field), 0))
    if paper.evidence_level == "ABSTRACT_ONLY":
        review["evidence_quality"] = min(review["evidence_quality"], 55.0)
    warnings: list[str] = []
    if paper.evidence_level == "ABSTRACT_ONLY":
        for field in REVIEW_FIELDS[6:]:
            review[field], field_warnings = sanitize_abstract_claim(str(review.get(field, "")))
            warnings.extend(field_warnings)
    review["evidence_level"] = paper.evidence_level
    review["fulltext_reviewed"] = paper.evidence_level == "FULLTEXT_READ"
    review["claim_warnings"] = list(dict.fromkeys(warnings))
    paper.review = review
    return review


def _empty_deep_dive(paper: Paper) -> dict[str, Any]:
    abstract_only = paper.evidence_level == "ABSTRACT_ONLY"
    return {
        "background": "",
        "knowledge_gap": "",
        "scientific_question": "",
        "central_hypothesis": "",
        "study_design": "",
        "innovations": [],
        "figure_walkthrough": [],
        "figure_limitations": "NOT_EVALUABLE：未取得可核验的全文图号/图注证据。" if abstract_only else "仅根据已解析正文/图注解释；未直接视觉读取图片像素。",
        "key_controls_and_rescues": [],
        "causal_chain": "",
        "strengths": [],
        "limitations": [],
        "topic_mapping": "",
        "actionable_ideas": [],
        "do_not_overclaim": [],
        "supervisor_brief": "",
        "evidence_level": paper.evidence_level,
        "figure_evidence_mode": "ABSTRACT_ONLY" if abstract_only else "TEXT_OR_LEGEND_ONLY",
        "model": "deterministic-fallback",
    }


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(value: Any, *, max_items: int = 20) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:max_items]


def _sanitize_abstract_deep_dive(deep: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    for field in DEEP_DIVE_TEXT_FIELDS:
        cleaned, field_warnings = sanitize_abstract_claim(_clean_string(deep.get(field)))
        deep[field] = cleaned
        warnings.extend(field_warnings)
    for field in ("innovations", "key_controls_and_rescues", "strengths", "limitations", "actionable_ideas", "do_not_overclaim"):
        cleaned_items: list[Any] = []
        for item in _clean_list(deep.get(field)):
            if isinstance(item, dict):
                cleaned_dict: dict[str, Any] = {}
                for key, value in item.items():
                    cleaned, field_warnings = sanitize_abstract_claim(_clean_string(value))
                    cleaned_dict[str(key)] = cleaned
                    warnings.extend(field_warnings)
                cleaned_items.append(cleaned_dict)
            else:
                cleaned, field_warnings = sanitize_abstract_claim(_clean_string(item))
                cleaned_items.append(cleaned)
                warnings.extend(field_warnings)
        deep[field] = cleaned_items
    # Abstract-only evidence cannot support a figure-by-figure interpretation.
    if deep.get("figure_walkthrough"):
        warnings.append("removed_unsupported_figure_walkthrough")
    deep["figure_walkthrough"] = []
    deep["figure_limitations"] = "NOT_EVALUABLE：当前仅有摘要证据，不能逐图解读。"
    deep["figure_evidence_mode"] = "ABSTRACT_ONLY"
    return deep, list(dict.fromkeys(warnings))


def _deep_dive_prompt(paper: Paper, profile: dict[str, Any], *, max_figures: int) -> str:
    if paper.evidence_level == "FULLTEXT_READ":
        evidence = _section_chunks(paper.fulltext_text, chunk_chars=5200, max_chunks=10)
    else:
        evidence = paper.abstract
    prior = {key: paper.review.get(key) for key in REVIEW_FIELDS if key in paper.review}
    schema = {
        "background": "领域背景与这篇文章所处位置",
        "knowledge_gap": "作者试图填补的知识缺口",
        "scientific_question": "核心科学问题",
        "central_hypothesis": "中心假设",
        "study_design": "从起点到结论的整体实验逻辑",
        "innovations": ["概念/机制/技术/实验设计创新，逐条"],
        "figure_walkthrough": [
            {
                "figure": "Figure 1",
                "question": "这张图回答什么",
                "approach": "做了什么实验",
                "key_result": "主要结果",
                "logic_role": "在全文因果链中的作用",
                "caveat": "该图的限制",
                "evidence_basis": "RESULTS_TEXT 或 FIGURE_LEGEND_TEXT"
            }
        ],
        "figure_limitations": "图像证据限制说明",
        "key_controls_and_rescues": ["关键 control / blockade / activation / rescue / epistasis 设计"],
        "causal_chain": "用箭头式语言概括作者真正支持的因果链，并区分相关性",
        "strengths": ["证据链强项"],
        "limitations": ["证据缺口/过度解释风险"],
        "topic_mapping": "具体映射到用户的 PYGL-efferocytosis 课题，不要泛泛而谈",
        "actionable_ideas": [
            {"idea": "可执行实验思路", "why": "为何值得搬", "priority": "HIGH/MEDIUM/LOW", "minimum_test": "最小验证实验"}
        ],
        "do_not_overclaim": ["这篇文章不能替用户证明什么"],
        "supervisor_brief": "用偏口语化中文写给导师的一段简洁汇报"
    }
    return (
        "你现在只对已经进入每日 Top 推荐的论文做 journal-club 级科研拆解。\n"
        "目标不是复述摘要，而是解释背景、问题、实验推进、创新、证据强弱、逐图逻辑，以及对用户课题真正可迁移的实验设计。\n"
        f"evidence_level={paper.evidence_level}\n"
        "冻结研究画像：" + json.dumps(profile, ensure_ascii=False, sort_keys=True) + "\n"
        "上一阶段审稿结果：" + json.dumps(prior, ensure_ascii=False, sort_keys=True) + "\n"
        "严格证据边界：\n"
        "1) 只能使用下面提供的证据，不得补写你记忆中的论文内容。\n"
        "2) ABSTRACT_ONLY 时 figure_walkthrough 必须为空，不能写图号、剂量、样本量或详细方法。\n"
        "3) FULLTEXT_READ 也只代表解析到了正文文本；当前流水线没有视觉读取原始 figure 图片。只有证据中明确出现 Figure/Fig 编号、Results 描述或 figure legend 时，才能生成对应 figure_walkthrough。\n"
        "4) 不得从正文推测图像中未被文字明确描述的条带、点图、显微图细节。\n"
        "5) 没有证据的项目写 NOT_EVALUABLE，不要填充。\n"
        f"6) figure_walkthrough 最多 {max_figures} 项，按论文逻辑顺序；没有可核验图号就留空。\n"
        "7) actionable_ideas 要直接对应用户课题的 upstream→PYGL(S15)→glycogenolysis→efferosome/phagolysosome processing，以及 rescue/epistasis/injury-resolution 设计；不能把外部论文当成用户体系已经证明的事实。\n"
        "8) supervisor_brief 可以口语化，但仍要保持证据边界。\n"
        "只输出一个 JSON 对象，不要 Markdown。JSON schema 示例：" + json.dumps(schema, ensure_ascii=False) + "\n"
        f"题名：{paper.title}\n期刊：{paper.journal}\n证据：{evidence}"
    )


def deep_dive_paper(
    paper: Paper,
    client: LLMClient,
    profile: dict[str, Any] | None = None,
    *,
    max_tokens: int = 7000,
    max_figures: int = 12,
) -> dict[str, Any]:
    """Add a detailed, evidence-bounded journal-club report to a Top paper."""
    profile = profile or {}
    deep = _empty_deep_dive(paper)
    warnings: list[str] = []
    try:
        response = client.generate(
            _deep_dive_prompt(paper, profile, max_figures=max_figures),
            system=(
                "Return valid JSON only. Evidence boundaries are mandatory. "
                "Do not claim visual inspection of figures; the provided evidence is text/legend only."
            ),
            max_tokens=max_tokens,
        )
        parsed = parse_json_response(response)
        if isinstance(parsed, dict):
            for field in DEEP_DIVE_TEXT_FIELDS:
                if field in parsed:
                    deep[field] = _clean_string(parsed.get(field))
            for field in DEEP_DIVE_LIST_FIELDS:
                if field in parsed:
                    deep[field] = _clean_list(parsed.get(field), max_items=max_figures if field == "figure_walkthrough" else 20)
            deep["model"] = getattr(client, "model", "configured-llm")
    except Exception as exc:
        logger.warning("Detailed review failed for %s; keeping bounded fallback: %s", paper.title, exc)

    if paper.evidence_level == "ABSTRACT_ONLY":
        deep, warnings = _sanitize_abstract_deep_dive(deep)
    else:
        cleaned_figures: list[dict[str, str]] = []
        for item in _clean_list(deep.get("figure_walkthrough"), max_items=max_figures):
            if not isinstance(item, dict):
                continue
            figure = _clean_string(item.get("figure"))
            if not re.search(r"\b(?:fig(?:ure)?\.?\s*)\d+", figure, re.I):
                continue
            cleaned_figures.append({
                "figure": figure,
                "question": _clean_string(item.get("question")),
                "approach": _clean_string(item.get("approach")),
                "key_result": _clean_string(item.get("key_result")),
                "logic_role": _clean_string(item.get("logic_role")),
                "caveat": _clean_string(item.get("caveat")),
                "evidence_basis": _clean_string(item.get("evidence_basis")) or "TEXT_OR_LEGEND_ONLY",
            })
        deep["figure_walkthrough"] = cleaned_figures
        deep["figure_evidence_mode"] = "TEXT_OR_LEGEND_ONLY"
        if not cleaned_figures:
            deep["figure_limitations"] = deep.get("figure_limitations") or "NOT_EVALUABLE：解析文本中没有足够可核验的图号/图注。"

    deep["evidence_level"] = paper.evidence_level
    deep["claim_warnings"] = warnings
    paper.review["deep_dive"] = deep
    paper.review["deep_dive_provider"] = "configured-llm"
    return deep
