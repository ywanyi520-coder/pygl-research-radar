from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HTTPResponse:
    status: int
    body: str | bytes
    headers: dict[str, str] = field(default_factory=dict)


class HTTPClient:
    """Small injectable HTTP client; no credentials are included in logs."""

    def __init__(self, timeout: float = 30.0, user_agent: str = "pygl-research-radar/1.0"):
        self.timeout = timeout
        self.user_agent = user_agent

    def request(self, url: str, *, params: dict[str, Any] | None = None, method: str = "GET", data: bytes | None = None) -> HTTPResponse:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"User-Agent": self.user_agent, "Accept": "application/json, text/xml, text/html, application/pdf"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                headers = dict(response.headers.items())
                content_type = " ".join(f"{key}:{value}" for key, value in headers.items()).casefold()
                body: str | bytes = raw if "application/pdf" in content_type or raw.startswith(b"%PDF") else raw.decode("utf-8", errors="replace")
                return HTTPResponse(response.status, body, headers)
        except Exception as exc:
            logger.warning("HTTP request failed for %s: %s", urllib.parse.urlsplit(url).netloc, exc)
            return HTTPResponse(0, "", {})

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.request(url, params=params)
        if response.status < 200 or response.status >= 300:
            return {}
        try:
            value = json.loads(response.body)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            logger.warning("Invalid JSON response from %s", urllib.parse.urlsplit(url).netloc)
            return {}

    def get_text(self, url: str, *, params: dict[str, Any] | None = None) -> HTTPResponse:
        return self.request(url, params=params)

    def get_binary(self, url: str, *, params: dict[str, Any] | None = None) -> HTTPResponse:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/pdf, text/html, application/xml"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return HTTPResponse(response.status, response.read(), dict(response.headers.items()))
        except Exception as exc:
            logger.warning("HTTP binary request failed for %s: %s", urllib.parse.urlsplit(url).netloc, exc)
            return HTTPResponse(0, b"", {})
