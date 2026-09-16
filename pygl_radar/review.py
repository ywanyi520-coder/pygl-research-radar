from __future__ import annotations

import logging
import json
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
    """Build a review evidence map that samples the whole article, not only its front.

    OA converters do not all preserve headings.  We therefore use named sections
    when available and deterministic evenly-spaced chunks as a safe fallback.
    """
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    positions: list[tuple[int, str]] = []
    for match in re.finditer(r"(?i)\b(abstract|introduction|materials and methods|methods|results|discussion|conclusions?|limitations)\b", normalized):
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
        # Evidence quality is itself evidence-dependent; an LLM cannot raise
        # it to full-text confidence merely by returning a larger number.
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
