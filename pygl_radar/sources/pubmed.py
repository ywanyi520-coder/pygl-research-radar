from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Paper
from .common import HTTPClient

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def _date_string(value: str) -> str | None:
    text = re.sub(r"\s+", " ", value or "").strip()
    match = re.search(r"(\d{4})(?:\s+([A-Za-z]{3}|\d{1,2}))?(?:\s+(\d{1,2}))?", text)
    if not match:
        return None
    year, month, day = match.groups()
    if not month:
        return year
    months = {name: index for index, name in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
    month_num = months.get(month[:3].title())
    if month_num is None:
        month_num = int(month) if month.isdigit() else 1
    return f"{year}-{int(month_num):02d}-{int(day or 1):02d}"


def _text(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _query(config: dict[str, Any], start: datetime, end: datetime) -> str:
    profile = config.get("profile", {})
    terms = list(dict.fromkeys(profile.get("keywords", []) + profile.get("mechanisms", [])[:12]))
    joined = " OR ".join(f'"{term}"' for term in terms if term)
    return f"({joined}) AND ({start:%Y/%m/%d}[Date - Publication] : {end:%Y/%m/%d}[Date - Publication])"


def search_pubmed(config: dict[str, Any], *, now: datetime | None = None, http: HTTPClient | None = None) -> list[Paper]:
    now = now or datetime.now(timezone.utc)
    http = http or HTTPClient()
    settings = config.get("sources", {}).get("pubmed", {})
    start = now - timedelta(hours=int(config.get("sources", {}).get("window_hours", 48)))
    params = {
        "db": "pubmed", "term": _query(config, start, now),
        "retmax": int(settings.get("max_results", 80)), "retmode": "json", "sort": "date",
    }
    if settings.get("email"):
        params["email"] = settings["email"]
    if settings.get("api_key"):
        params["api_key"] = settings["api_key"]
    result = http.get_json(ESEARCH_URL, params=params)
    ids = ((result.get("esearchresult") or {}).get("idlist") or [])
    if not ids:
        return []
    response = http.get_text(EFETCH_URL, params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
    if response.status < 200 or response.status >= 300 or not response.body:
        logger.warning("PubMed fetch returned HTTP %s", response.status)
        return []
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError:
        logger.warning("PubMed returned malformed XML")
        return []
    papers: list[Paper] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.find(".//PMID"))
        title = _text(article.find(".//ArticleTitle"))
        abstract = " ".join(_text(node) for node in article.findall(".//AbstractText")).strip()
        journal = _text(article.find(".//Journal/Title"))
        pub_date = article.find(".//JournalIssue/PubDate")
        publication_date = _date_string(_text(pub_date))
        doi = None
        pmcid = None
        for identifier in article.findall(".//ArticleId"):
            identifier_type = identifier.attrib.get("IdType", "").lower()
            if identifier_type == "doi":
                doi = _text(identifier)
            elif identifier_type == "pmc":
                pmcid = _text(identifier)
        authors = []
        for author in article.findall(".//Author"):
            name = " ".join(filter(None, [_text(author.find("ForeName")), _text(author.find("LastName"))]))
            if name:
                authors.append(name)
        if title:
            papers.append(Paper(
                title=title, abstract=abstract, journal=journal, publication_date=publication_date,
                doi=doi, pmid=pmid, pmcid=pmcid, publisher_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                source_urls=[f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"] if pmid else [],
                sources=["pubmed"], authors=authors,
            ))
    return papers
