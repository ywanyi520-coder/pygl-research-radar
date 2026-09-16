from __future__ import annotations

import html
import logging
import re
import tempfile
from dataclasses import dataclass
from urllib.parse import quote

from .models import Paper
from .sources.common import HTTPClient, HTTPResponse

logger = logging.getLogger(__name__)

BLOCKED_MARKERS = (
    "just a moment", "challenge-platform", "cf-mitigated", "captcha",
    "verify you are human", "access denied", "paywall", "subscription required",
)
ARTICLE_BODY_MARKERS = ("<article", "article-body", "article_body", "fulltext", "full-text")


@dataclass
class FulltextResult:
    evidence_level: str
    text: str = ""
    source_url: str | None = None
    retrieval_mode: str | None = None
    reason: str = ""


def _body_text(body: str | bytes) -> str:
    return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body


def _blocked(body: str | bytes) -> bool:
    lowered = _body_text(body or "").casefold()
    return any(marker in lowered for marker in BLOCKED_MARKERS)


def _xml_or_html_text(body: str | bytes) -> str:
    value = html.unescape(_body_text(body or ""))
    value = re.sub(r"</?(?:title|h[1-6]|sec|section)[^>]*>", "\n", value, flags=re.I)
    value = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _pdf_text(body: str | bytes) -> str:
    """Extract PDF text with PyMuPDF when available, otherwise declared pypdf."""
    raw = body if isinstance(body, bytes) else body.encode("latin1", errors="ignore")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(raw)
        handle.flush()
        try:
            import fitz  # type: ignore
            with fitz.open(handle.name) as document:
                return "\n".join(page.get_text() for page in document).strip()
        except Exception:
            pass
        try:
            from pypdf import PdfReader  # type: ignore
            return "\n".join(page.extract_text() or "" for page in PdfReader(handle.name).pages).strip()
        except Exception:
            return ""


def _content_text(response: HTTPResponse) -> str:
    content_type = " ".join(f"{key}:{value}" for key, value in response.headers.items()).casefold()
    if (isinstance(response.body, bytes) and response.body.startswith(b"%PDF")) or (isinstance(response.body, str) and response.body.startswith("%PDF")) or "application/pdf" in content_type:
        return _pdf_text(response.body)
    return _xml_or_html_text(response.body)


def _is_pdf(response: HTTPResponse) -> bool:
    content_type = " ".join(f"{key}:{value}" for key, value in response.headers.items()).casefold()
    body = response.body
    return (isinstance(body, bytes) and body.startswith(b"%PDF")) or (isinstance(body, str) and body.startswith("%PDF")) or "application/pdf" in content_type


def _is_xml_fulltext(body: str | bytes) -> bool:
    value = _body_text(body or "")
    lowered = value.casefold()
    return "<body" in lowered and ("<article" in lowered or "<article-meta" in lowered or "<sec" in lowered)


def _is_article_html(body: str | bytes) -> bool:
    value = _body_text(body or "")
    lowered = value.casefold()
    marker_count = sum(marker in lowered for marker in ARTICLE_BODY_MARKERS)
    headings = len(re.findall(r"<h[1-6]\b|<section\b|<div[^>]+class=[\"'][^\"']*(?:section|article)[^\"']*[\"']", lowered))
    named_section = bool(re.search(r"<(?:h[1-6]|section|div)[^>]*>[^<]*(?:results|methods?|materials|discussion|conclusions?|limitations)[^<]*<", lowered, re.I | re.S))
    return marker_count >= 1 and headings >= 2 and named_section


class FulltextAcquirer:
    """Lawful OA-only acquisition chain; never bypasses a paywall or anti-bot gate."""

    def __init__(self, http: HTTPClient | None = None, *, timeout: float = 30.0, min_text_chars: int = 200, min_pdf_text_chars: int | None = None, min_html_text_chars: int | None = None):
        self.http = http or HTTPClient(timeout=timeout)
        self.min_text_chars = min_text_chars
        self.min_pdf_text_chars = min_pdf_text_chars if min_pdf_text_chars is not None else max(800, min_text_chars)
        self.min_html_text_chars = min_html_text_chars if min_html_text_chars is not None else max(2000, min_text_chars)

    def _url(self, url: str, *, mode: str) -> FulltextResult | None:
        get_binary = getattr(self.http, "get_binary", None)
        # Binary-first prevents a PDF with a non-.pdf URL from being UTF-8 decoded.
        response = get_binary(url) if get_binary else self.http.get_text(url)
        if response.status < 200 or response.status >= 300 or not response.body:
            return None
        if _blocked(response.body):
            logger.info("Skipping blocked/paywalled full-text response from %s", url)
            return None
        text = _content_text(response)
        if _is_pdf(response):
            valid = len(text) >= self.min_pdf_text_chars
        elif mode.endswith("-xml") or mode == "pmc-xml":
            valid = _is_xml_fulltext(response.body) and len(text) >= self.min_text_chars
        else:
            # Unpaywall landing pages are not evidence.  HTML must expose an
            # article-body/section structure before it can be considered OA text.
            valid = _is_article_html(response.body) and len(text) >= self.min_html_text_chars
        if not valid:
            return None
        return FulltextResult("FULLTEXT_READ", text, url, mode, "retrieved and parsed")

    def _europe_pmc(self, pmid: str) -> FulltextResult | None:
        result = self.http.get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"EXT_ID:{pmid} AND SRC:MED", "format": "json", "pageSize": 1},
        )
        hits = ((result.get("resultList") or {}).get("result") or [])
        pmcid = hits[0].get("pmcid") if hits and isinstance(hits[0], dict) else None
        if pmcid:
            found = self._url(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
                mode="europepmc-xml",
            )
            if found:
                return found
        if hits and isinstance(hits[0], dict):
            for link in (hits[0].get("fullTextUrlList") or {}).get("fullTextUrl", []):
                if isinstance(link, dict) and link.get("url"):
                    found = self._url(str(link["url"]), mode="europepmc-oa-link")
                    if found:
                        return found
        return None

    def _pmc(self, pmcid: str) -> FulltextResult | None:
        return self._url(
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{quote(pmcid, safe='')}/?report=xml",
            mode="pmc-xml",
        )

    def _unpaywall(self, doi: str, email: str) -> FulltextResult | None:
        result = self.http.get_json(
            f"https://api.unpaywall.org/v2/{quote(doi, safe='')}", params={"email": email}
        )
        locations = result.get("oa_locations") or []
        ordered = sorted(locations, key=lambda item: (not bool(item.get("is_best")), not bool(item.get("host_type") == "repository")))
        for location in ordered:
            for key in ("url_for_pdf", "url_for_landing_page"):
                url = location.get(key)
                if url:
                    found = self._url(str(url), mode=f"unpaywall-{key}")
                    if found:
                        return found
        return None

    def acquire(self, paper: Paper, *, unpaywall_email: str = "") -> FulltextResult:
        if paper.pmcid:
            found = self._pmc(str(paper.pmcid))
            if found:
                return found
        if paper.pmid:
            found = self._europe_pmc(str(paper.pmid))
            if found:
                return found
        if paper.doi and unpaywall_email:
            found = self._unpaywall(str(paper.doi), unpaywall_email)
            if found:
                return found
        # Crossref links can be OA publisher/repository links; only use links
        # explicitly supplied by the source and abandon blocked responses.
        for url in paper.source_urls:
            if url and url not in {paper.publisher_url} and ("pdf" in url.casefold() or "oa" in url.casefold()):
                found = self._url(url, mode="source-oa-link")
                if found:
                    return found
        return FulltextResult("ABSTRACT_ONLY", reason="no lawful parseable full text available")


def apply_fulltext(paper: Paper, acquirer: FulltextAcquirer, *, unpaywall_email: str = "") -> FulltextResult:
    result = acquirer.acquire(paper, unpaywall_email=unpaywall_email)
    paper.evidence_level = "FULLTEXT_READ" if result.evidence_level == "FULLTEXT_READ" else "ABSTRACT_ONLY"
    paper.fulltext_source_url = result.source_url
    paper.fulltext_retrieval_mode = result.retrieval_mode
    paper.fulltext_text = result.text if paper.evidence_level == "FULLTEXT_READ" else ""
    return result
