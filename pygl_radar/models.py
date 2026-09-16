from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import re
from typing import Any, Literal

EvidenceLevel = Literal["FULLTEXT_READ", "ABSTRACT_ONLY"]


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


@dataclass
class Paper:
    """Canonical paper record shared by every pipeline stage."""

    title: str
    abstract: str = ""
    journal: str = ""
    publication_date: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    publisher_url: str | None = None
    source_urls: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    evidence_level: EvidenceLevel = "ABSTRACT_ONLY"
    fulltext_source_url: str | None = None
    fulltext_retrieval_mode: str | None = None
    fulltext_text: str = field(default="", repr=False)
    triage: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    final_score: float = 0.0

    def __post_init__(self) -> None:
        if self.evidence_level not in {"FULLTEXT_READ", "ABSTRACT_ONLY"}:
            self.evidence_level = "ABSTRACT_ONLY"

    def key_candidates(self) -> list[str]:
        keys: list[str] = []
        if self.doi:
            doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", self.doi.strip().lower()).removeprefix("doi:")
            keys.append(f"doi:{doi}")
        if self.pmid:
            keys.append(f"pmid:{''.join(ch for ch in str(self.pmid) if ch.isdigit())}")
        return keys

    def to_dict(self, *, include_fulltext: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_fulltext:
            data.pop("fulltext_text", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Paper":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in data.items() if key in allowed}
        values.setdefault("title", "")
        return cls(**values)


@dataclass(frozen=True)
class ScoreBreakdown:
    direct_relevance: float
    mechanism_relevance: float
    experimental_similarity: float
    transferability: float
    idea_value: float
    evidence_quality: float
    final_score: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)
