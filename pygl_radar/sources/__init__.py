from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..models import Paper
from .common import HTTPClient
from .crossref import search_crossref, search_crossref_journal_lane
from .pubmed import search_pubmed, search_pubmed_journal_lane

logger = logging.getLogger(__name__)


def collect_candidates(config: dict[str, Any], *, now: datetime | None = None, http: HTTPClient | None = None) -> tuple[list[Paper], dict[str, Any]]:
    """Run independent sources; one source failure never discards the other."""
    client = http or HTTPClient()
    papers: list[Paper] = []
    counts: dict[str, Any] = {"retrieved": {}, "failures": {}}
    source_settings = config.get("sources", {})
    for name, enabled, loader, journal_loader in (
        ("pubmed", bool(source_settings.get("pubmed", {}).get("enabled", True)), search_pubmed, search_pubmed_journal_lane),
        ("crossref", bool(source_settings.get("crossref", {}).get("enabled", True)), search_crossref, search_crossref_journal_lane),
    ):
        if not enabled:
            counts["retrieved"][f"{name}_topic"] = 0
            counts["retrieved"][f"{name}_journal"] = 0
            continue
        try:
            found = loader(config, now=now, http=client, lane="topic")
            papers.extend(found)
            counts["retrieved"][f"{name}_topic"] = len(found)
            if bool(source_settings.get("journal_lane", {}).get("enabled", True)):
                journal_found = journal_loader(config, now=now, http=client)
                papers.extend(journal_found)
                counts["retrieved"][f"{name}_journal"] = len(journal_found)
            else:
                counts["retrieved"][f"{name}_journal"] = 0
            counts["retrieved"][name] = len(found) + len(journal_found if bool(source_settings.get("journal_lane", {}).get("enabled", True)) else [])
        except Exception as exc:  # defensive source boundary
            counts["failures"][name] = str(exc)
            counts["retrieved"][f"{name}_topic"] = 0
            counts["retrieved"][f"{name}_journal"] = 0
            counts["retrieved"][name] = counts["retrieved"].get(f"{name}_topic", 0)
            logger.exception("Source %s failed; continuing with partial results", name)
    return papers, counts


__all__ = ["collect_candidates", "search_crossref", "search_crossref_journal_lane", "search_pubmed", "search_pubmed_journal_lane"]
