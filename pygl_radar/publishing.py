from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class PublicationResult:
    ok: bool
    url: str = ""
    issue_number: int | None = None
    error: str | None = None


class ReportPublisher(Protocol):
    def publish(self, markdown: str, *, report_date: str) -> PublicationResult: ...


class NoopPublisher:
    def publish(self, markdown: str, *, report_date: str) -> PublicationResult:
        return PublicationResult(ok=True)


class GitHubIssuePublisher:
    """Publish one mobile-readable daily report as a GitHub Issue."""

    def __init__(self, token: str, repository: str, *, api_url: str = "https://api.github.com"):
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubIssuePublisher":
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
        missing = [name for name, value in (("GITHUB_TOKEN", token), ("GITHUB_REPOSITORY", repository)) if not value]
        if missing:
            raise ValueError("Missing report publishing environment: " + ", ".join(missing))
        return cls(token, repository, api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").strip())

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.api_url}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                "User-Agent": "pygl-research-radar/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value

    def _existing_issue(self, title: str) -> dict[str, Any] | None:
        value = self._request("GET", f"/repos/{self.repository}/issues?state=all&per_page=100")
        if not isinstance(value, list):
            raise ValueError("GitHub Issue listing response was not a list")
        for item in value:
            if isinstance(item, dict) and not item.get("pull_request") and item.get("title") == title and isinstance(item.get("number"), int):
                return item
        return None

    def publish(self, markdown: str, *, report_date: str) -> PublicationResult:
        try:
            title = f"PYGL Research Radar — {report_date}"
            existing = self._existing_issue(title)
            if existing:
                number = int(existing["number"])
                value = self._request("PATCH", f"/repos/{self.repository}/issues/{number}", {"body": markdown, "state": "open"})
            else:
                value = self._request("POST", f"/repos/{self.repository}/issues", {"title": title, "body": markdown})
            if not isinstance(value, dict):
                return PublicationResult(False, error="GitHub Issue response was not an object")
            url = str(value.get("html_url") or "")
            number = value.get("number")
            if not url or not isinstance(number, int):
                return PublicationResult(False, error="GitHub Issue response missing URL")
            return PublicationResult(True, url=url, issue_number=number)
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Report publication failed: %s", type(exc).__name__)
            return PublicationResult(False, error="GitHub Issue publication failed")


def fetch_github_issue_comments(token: str, repository: str, issue_number: int, *, api_url: str = "https://api.github.com") -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/repos/{repository}/issues/{issue_number}/comments",
        headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pygl-research-radar/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
