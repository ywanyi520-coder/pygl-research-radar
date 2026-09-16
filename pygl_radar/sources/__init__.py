from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..models import Paper
from .common import HTTPClient
from .crossref import search_crossref
from .pubmed import search_pubmed

logger = logging.getLogger(__name__)


def collect_candidates(config: dict[str, Any], *, now: datetime | None = None, http: HTTPClient | None = None) -> tuple[list[Paper], dict[str, Any]]:
    """Run independent sources; one source failure never discards the other."""
    client = http or HTTPClient()
    papers: list[Paper] = []
    counts: dict[str, Any] = {"retrieved": {}, "failures": {}}
    source_settings = config.get("sources", {})
    for name, enabled, loader in (
        ("pubmed", bool(source_settings.get("pubmed", {}).get("enabled", True)), search_pubmed),
        ("crossref", bool(source_settings.get("crossref", {}).get("enabled", True)), search_crossref),
    ):
        if not enabled:
            counts["retrieved"][name] = 0
            continue
        try:
            found = loader(config, now=now, http=client)
            papers.extend(found)
            counts["retrieved"][name] = len(found)
        except Exception as exc:  # defensive source boundary
            counts["failures"][name] = str(exc)
            counts["retrieved"][name] = 0
            logger.exception("Source %s failed; continuing with partial results", name)
    return papers, counts


__all__ = ["collect_candidates", "search_crossref", "search_pubmed"]
