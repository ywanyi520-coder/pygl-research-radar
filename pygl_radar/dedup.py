from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable

from .models import Paper

logger = logging.getLogger(__name__)


def canonical_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = value.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = doi.removeprefix("doi:").strip().rstrip(".")
    return doi or None


def canonical_pmid(value: str | None) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits or None


def canonical_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


def paper_identity(paper: Paper) -> str:
    doi = canonical_doi(paper.doi)
    if doi:
        return f"doi:{doi}"
    pmid = canonical_pmid(paper.pmid)
    if pmid:
        return f"pmid:{pmid}"
    return f"title:{canonical_title(paper.title)}"


def _merge(target: Paper, incoming: Paper) -> None:
    if not target.abstract and incoming.abstract:
        target.abstract = incoming.abstract
    if not target.journal and incoming.journal:
        target.journal = incoming.journal
    if not target.publication_date and incoming.publication_date:
        target.publication_date = incoming.publication_date
    if not target.doi and incoming.doi:
        target.doi = incoming.doi
    if not target.pmid and incoming.pmid:
        target.pmid = incoming.pmid
    if not target.pmcid and incoming.pmcid:
        target.pmcid = incoming.pmcid
    if not target.publisher_url and incoming.publisher_url:
        target.publisher_url = incoming.publisher_url
    target.authors = list(dict.fromkeys(target.authors + incoming.authors))
    target.source_urls = list(dict.fromkeys(target.source_urls + incoming.source_urls))
    target.sources = list(dict.fromkeys(target.sources + incoming.sources))


def deduplicate_papers(papers: Iterable[Paper]) -> list[Paper]:
    """Deduplicate by DOI, then PMID, then normalized title, while merging metadata."""
    result: list[Paper] = []
    index: dict[str, int] = {}
    for paper in papers:
        paper.doi = canonical_doi(paper.doi)
        paper.pmid = canonical_pmid(paper.pmid)
        keys = paper.key_candidates() + [f"title:{canonical_title(paper.title)}"]
        existing_index = next((index[key] for key in keys if key in index), None)
        if existing_index is None:
            index_value = len(result)
            result.append(paper)
            for key in keys:
                index[key] = index_value
        else:
            _merge(result[existing_index], paper)
            for key in result[existing_index].key_candidates() + [
                f"title:{canonical_title(result[existing_index].title)}"
            ]:
                index[key] = existing_index
    return result


class SeenCache:
    """Small JSON cache used only for papers already successfully pushed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.ids = set(str(item) for item in (data.get("pushed", []) if isinstance(data, dict) else []))

    def contains(self, paper: Paper) -> bool:
        keys = paper.key_candidates() or [f"title:{canonical_title(paper.title)}"]
        return any(key in self.ids for key in keys)

    def mark(self, papers: Iterable[Paper]) -> None:
        for paper in papers:
            self.ids.update(paper.key_candidates() or [f"title:{canonical_title(paper.title)}"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "pushed": sorted(self.ids)}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
