from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Paper
from .common import HTTPClient

logger = logging.getLogger(__name__)

WORKS_URL = "https://api.crossref.org/works"


def _strip_markup(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _date(item: dict[str, Any]) -> str | None:
    for key in ("published", "published-online", "published-print", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            values = [int(part) for part in parts[:3]]
            while len(values) < 3:
                values.append(1)
            return f"{values[0]:04d}-{values[1]:02d}-{values[2]:02d}"
    return None


def search_crossref(config: dict[str, Any], *, now: datetime | None = None, http: HTTPClient | None = None) -> list[Paper]:
    now = now or datetime.now(timezone.utc)
    http = http or HTTPClient()
    settings = config.get("sources", {}).get("crossref", {})
    start = now - timedelta(hours=int(config.get("sources", {}).get("window_hours", 48)))
    profile = config.get("profile", {})
    terms = list(dict.fromkeys(profile.get("keywords", []) + profile.get("mechanisms", [])[:8]))
    result = http.get_json(WORKS_URL, params={
        "query.bibliographic": " ".join(terms),
        "filter": f"from-pub-date:{start:%Y-%m-%d},until-pub-date:{now:%Y-%m-%d}",
        "rows": int(settings.get("max_results", 80)), "select": "DOI,title,abstract,container-title,published,published-online,published-print,URL,author,link",
        "mailto": settings.get("email", ""),
    })
    items = ((result.get("message") or {}).get("items") or [])
    papers: list[Paper] = []
    for item in items:
        titles = item.get("title") or []
        title = str(titles[0]).strip() if titles else ""
        if not title:
            continue
        doi = item.get("DOI")
        publisher_url = item.get("URL") or (f"https://doi.org/{doi}" if doi else None)
        links = [str(link.get("URL")) for link in (item.get("link") or []) if link.get("URL")]
        authors = []
        for author in item.get("author") or []:
            name = " ".join(filter(None, [author.get("given"), author.get("family")]))
            if name:
                authors.append(name)
        papers.append(Paper(
            title=title, abstract=_strip_markup(str(item.get("abstract", ""))),
            journal=str((item.get("container-title") or [""])[0]), publication_date=_date(item),
            doi=doi, publisher_url=publisher_url, source_urls=list(dict.fromkeys([u for u in [publisher_url, *links] if u])),
            sources=["crossref"], authors=authors,
        ))
    return papers
