from __future__ import annotations

from dataclasses import asdict
from typing import Any, Iterable

from .models import Paper, ScoreBreakdown

SCORE_FIELDS = (
    "direct_relevance", "mechanism_relevance", "experimental_similarity",
    "transferability", "idea_value", "evidence_quality",
)
DEFAULT_WEIGHTS = {
    "direct_relevance": 0.12,
    "mechanism_relevance": 0.14,
    "experimental_similarity": 0.24,
    "transferability": 0.22,
    "idea_value": 0.18,
    "evidence_quality": 0.10,
}


def clamp_score(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def normalize_weights(weights: dict[str, Any] | None) -> dict[str, float]:
    raw = {key: clamp_score(value) for key, value in {**DEFAULT_WEIGHTS, **(weights or {})}.items()}
    result = {key: raw.get(key, 0.0) / 100.0 for key in SCORE_FIELDS}
    total = sum(result.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {key: value / total for key, value in result.items()}


def aggregate_scores(subscores: dict[str, Any], weights: dict[str, Any] | None = None) -> ScoreBreakdown:
    normalized = normalize_weights(weights)
    values = {field: clamp_score(subscores.get(field, 0.0)) for field in SCORE_FIELDS}
    final = round(sum(values[field] * normalized[field] for field in SCORE_FIELDS), 2)
    return ScoreBreakdown(**values, final_score=final)


def score_paper(paper: Paper, weights: dict[str, Any] | None = None, feedback_delta: float = 0.0) -> ScoreBreakdown:
    merged = {}
    merged.update(paper.triage or {})
    merged.update(paper.review or {})
    breakdown = aggregate_scores(merged, weights)
    final = round(max(0.0, min(100.0, breakdown.final_score + float(feedback_delta))), 2)
    paper.scores = {field: getattr(breakdown, field) for field in SCORE_FIELDS}
    paper.final_score = final
    return ScoreBreakdown(**paper.scores, final_score=final)


def rank_papers(papers: Iterable[Paper], *, top_n: int = 5, min_score: float = 50.0) -> list[Paper]:
    ranked = sorted(papers, key=lambda paper: paper.final_score, reverse=True)
    return [paper for paper in ranked if paper.final_score >= min_score][: max(0, top_n)]
