from __future__ import annotations

import logging
import json
import re
from typing import Any, Iterable

from .llm import LLMClient, parse_json_response
from .models import Paper

logger = logging.getLogger(__name__)

TRIAGE_FIELDS = ("direct_relevance", "mechanism_relevance", "experimental_similarity", "transferability", "idea_value", "evidence_quality")


def _controlled_tags(values: Any, vocabulary: list[Any]) -> list[str]:
    """Whitelist LLM tags against the frozen profile vocabulary."""
    if not isinstance(values, list):
        return []
    allowed = {re.sub(r"\s+", " ", str(value).casefold()).strip(): str(value) for value in vocabulary if str(value).strip()}
    result: list[str] = []
    for value in values:
        key = re.sub(r"\s+", " ", str(value).casefold()).strip()
        if key in allowed:
            result.append(allowed[key])
    return list(dict.fromkeys(result))


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return default


def _signals(paper: Paper, profile: dict[str, Any]) -> tuple[int, int, int]:
    # Do not reward a candidate for a pattern that the abstract explicitly says
    # was not tested (a common failure mode in keyword-only ranking).
    sentences = re.split(r"(?<=[.!?。！？])\s+", f"{paper.title}. {paper.abstract}")
    positive_sentences = [
        sentence for sentence in sentences
        if not re.search(r"\b(?:no|not|without|lacks?|failed to|did not|does not|cannot|absence of|neither)\b|未|无|不涉及|未检验|没有", sentence, re.I)
    ]
    text = " ".join(positive_sentences).casefold()
    direct = sum(1 for term in profile.get("keywords", []) if str(term).casefold() in text)
    mechanisms = sum(1 for term in profile.get("mechanisms", []) if str(term).casefold() in text)
    patterns = sum(1 for term in profile.get("experimental_patterns", []) if str(term).casefold() in text)
    return direct, mechanisms, patterns


def deterministic_triage(paper: Paper, profile: dict[str, Any]) -> dict[str, Any]:
    """Recall-first fallback for runs without an LLM or with a malformed response."""
    direct, mechanisms, patterns = _signals(paper, profile)
    text = f"{paper.title} {paper.abstract}".casefold()
    matched_mechanisms = [term for term in profile.get("mechanisms", []) if str(term).casefold() in text][:6]
    matched_patterns = [term for term in profile.get("experimental_patterns", []) if str(term).casefold() in text][:8]
    journal_lane = any(str(source).endswith("-journal") for source in paper.sources)
    return {
        # Journal-first papers without topic terms must still reach the LLM;
        # an offline fallback cannot score them positively, so they remain
        # eligible for later review but will not pass a score threshold by
        # keyword accident.
        "retain": bool(direct or mechanisms or patterns or journal_lane),
        "direct_relevance": min(100.0, 28.0 * direct),
        "mechanism_relevance": min(100.0, 22.0 * mechanisms),
        # Pattern hits intentionally have more leverage than raw keyword hits.
        "experimental_similarity": min(100.0, 30.0 * patterns),
        "transferability": min(100.0, 24.0 * patterns + 8.0 * mechanisms),
        "idea_value": min(100.0, 20.0 * patterns + 10.0 * mechanisms),
        "evidence_quality": 35.0 if paper.abstract else 15.0,
        "rationale": "基于题名/摘要的确定性召回；待深审确认。",
        "matched_mechanisms": matched_mechanisms,
        "matched_patterns": matched_patterns,
        "model": "deterministic-fallback",
    }


def _prompt(papers: list[Paper], profile: dict[str, Any]) -> str:
    records = []
    for index, paper in enumerate(papers):
        records.append({"index": index, "title": paper.title, "abstract": paper.abstract[:6000]})
    return (
        "你是生物医学文献初筛器。只根据给出的题名和摘要判断，不得补写全文细节。\n"
        "研究画像：" + json.dumps(profile, ensure_ascii=False, sort_keys=True) + "\n"
        "实验模式优先于关键词重合；即使没有出现 PYGL，只要因果/实验结构可迁移也可保留。\n"
        "matched_mechanisms 和 matched_patterns 必须从下列 profile 原词表逐字选择（允许大小写差异，禁止创造新标签）；语义相似时也要返回对应控制标签。\n"
        "机制词表：" + json.dumps(profile.get("mechanisms", []), ensure_ascii=False) + "\n"
        "实验模式词表：" + json.dumps(profile.get("experimental_patterns", []), ensure_ascii=False) + "\n"
        "对每篇返回 0-100 的六个分数，并返回 retain、rationale、matched_mechanisms、matched_patterns。只输出 JSON："
        "{\"items\":[{\"index\":0,\"retain\":true,\"direct_relevance\":0,"
        "\"mechanism_relevance\":0,\"experimental_similarity\":0,\"transferability\":0,"
        "\"idea_value\":0,\"evidence_quality\":0,\"rationale\":\"...\","
        "\"matched_mechanisms\":[],\"matched_patterns\":[]}]}\n"
        + str(records)
    )


def triage_batch(papers: Iterable[Paper], profile: dict[str, Any], client: LLMClient, *, batch_size: int = 20) -> list[Paper]:
    """Batch triage: at most one LLM request per batch, with safe fallback."""
    all_papers = list(papers)
    retained: list[Paper] = []
    for start in range(0, len(all_papers), max(1, batch_size)):
        batch = all_papers[start : start + max(1, batch_size)]
        parsed: dict[int, dict[str, Any]] = {}
        try:
            response = client.generate(
                _prompt(batch, profile),
                system="Return valid JSON only. Abstract-only evidence cannot support figure, dose, sample-size, or unshown method claims.",
                max_tokens=4000,
            )
            payload = parse_json_response(response)
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("index"), int):
                        parsed[item["index"]] = item
        except Exception as exc:
            logger.warning("Triage batch failed; using deterministic fallback: %s", exc)
        for index, paper in enumerate(batch):
            triage = deterministic_triage(paper, profile)
            if index in parsed:
                candidate = parsed[index]
                triage.update({field: _clamp(candidate.get(field), triage[field]) for field in TRIAGE_FIELDS})
                triage["retain"] = bool(candidate.get("retain", triage["retain"]))
                triage["rationale"] = str(candidate.get("rationale") or triage["rationale"])
                tag_source = "deterministic-fallback"
                if "matched_mechanisms" in candidate:
                    triage["matched_mechanisms"] = _controlled_tags(candidate.get("matched_mechanisms"), list(profile.get("mechanisms", [])))
                    tag_source = "llm-controlled"
                if "matched_patterns" in candidate:
                    triage["matched_patterns"] = _controlled_tags(candidate.get("matched_patterns"), list(profile.get("experimental_patterns", [])))
                    tag_source = "llm-controlled"
                triage["tag_source"] = tag_source
                triage["model"] = getattr(client, "model", "configured-llm")
            paper.triage = triage
            if triage.get("retain"):
                retained.append(paper)
    retained.sort(key=lambda paper: sum(float(paper.triage.get(field, 0)) for field in TRIAGE_FIELDS), reverse=True)
    return retained
