from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Paper

LABELS = {"relevant", "idea", "method", "skip"}
LABEL_DELTAS = {"relevant": 2.0, "idea": 2.5, "method": 2.0, "skip": -2.0}
FEEDBACK_RE = re.compile(r"(?i)(?:feedback\s+)?(?P<label>relevant|idea|method|skip)\s*[:=,\s]+(?P<id>(?:doi:)?10\.\S+|(?:pmid[:\s]?)?\d+)")
FEEDBACK_RE_REVERSED = re.compile(r"(?i)(?:feedback\s+)?(?P<id>(?:doi:)?10\.\S+|(?:pmid[:\s]?)?\d+)\s*[:=,\s]+(?P<label>relevant|idea|method|skip)")


def _key(paper_id: str) -> str:
    value = str(paper_id or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if value.startswith("doi:"):
        return "doi:" + value[4:].strip().rstrip(".,)")
    if value.startswith("pmid:"):
        value = value[5:]
    if value.isdigit():
        return "pmid:" + value
    return "doi:" + value.rstrip(".,)")


def _paper_tags(paper: Paper) -> list[str]:
    tags: list[str] = []
    if paper.journal:
        tags.append("journal:" + re.sub(r"\s+", " ", paper.journal.casefold()).strip())
    triage = paper.triage or {}
    for prefix, key in (("mechanism", "matched_mechanisms"), ("pattern", "matched_patterns")):
        for value in triage.get(key, []) or []:
            clean = re.sub(r"\s+", " ", str(value).casefold()).strip()
            if clean:
                tags.append(f"{prefix}:{clean}")
    return list(dict.fromkeys(tags))


class FeedbackState:
    """Versioned, append-only-enough feedback state with a deliberately soft effect."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": 1, "papers": {}, "preferences": {}, "catalog": {}, "events": [], "processed_comment_ids": [], "report_issues": []}
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
            self.data.setdefault("papers", {})
            self.data.setdefault("preferences", {})
            self.data.setdefault("catalog", {})
            self.data.setdefault("events", [])
            self.data.setdefault("processed_comment_ids", [])
            self.data.setdefault("report_issues", [])

    def remember_papers(self, papers: list[Paper]) -> None:
        for paper in papers:
            tags = _paper_tags(paper)
            for identifier in paper.key_candidates():
                self.data["catalog"][identifier] = {"tags": tags, "title": paper.title}

    def record(self, paper_id: str, label: str, *, timestamp: str | None = None, tags: list[str] | None = None, comment_id: str | None = None) -> None:
        label = str(label).strip().lower()
        if label not in LABELS:
            raise ValueError(f"label must be one of {sorted(LABELS)}")
        key = _key(paper_id)
        entry = self.data["papers"].setdefault(key, {name: 0 for name in LABELS})
        entry[label] = int(entry.get(label, 0)) + 1
        self.data["events"].append({"paper_id": key, "label": label, "timestamp": timestamp})
        resolved_tags = list(tags or self.data.get("catalog", {}).get(key, {}).get("tags", []))
        for tag in resolved_tags:
            preference = self.data["preferences"].setdefault(tag, {name: 0 for name in LABELS})
            preference[label] = int(preference.get(label, 0)) + 1
        if comment_id is not None:
            self.data["processed_comment_ids"] = list(dict.fromkeys(self.data.get("processed_comment_ids", []) + [str(comment_id)]))[-500:]

    def ingest_comments(self, comments: list[dict[str, Any]]) -> int:
        """Parse explicit ``feedback <doi-or-pmid> <label>`` issue comments once."""
        processed = set(str(value) for value in self.data.get("processed_comment_ids", []))
        ingested = 0
        for comment in comments:
            comment_id = str(comment.get("id", ""))
            if comment_id and comment_id in processed:
                continue
            matches = list(FEEDBACK_RE.finditer(str(comment.get("body", ""))))
            matches.extend(FEEDBACK_RE_REVERSED.finditer(str(comment.get("body", ""))))
            for match in matches:
                self.record(match.group("id"), match.group("label"), timestamp=str(comment.get("created_at") or ""), comment_id=comment_id or None)
                ingested += 1
            if comment_id:
                processed.add(comment_id)
        self.data["processed_comment_ids"] = list(processed)[-500:]
        return ingested

    def record_report_issue(self, report_date: str, issue_number: int, url: str) -> None:
        entries = [item for item in self.data.get("report_issues", []) if item.get("date") != report_date]
        entries.append({"date": report_date, "number": issue_number, "url": url})
        self.data["report_issues"] = entries[-30:]

    def modifier(self, paper: Any) -> float:
        identifiers = []
        if getattr(paper, "doi", None):
            identifiers.append(_key(paper.doi))
        if getattr(paper, "pmid", None):
            identifiers.append(_key(paper.pmid))
        delta = 0.0
        for identifier in identifiers:
            entry = self.data.get("papers", {}).get(identifier, {})
            for label, amount in entry.items():
                delta += LABEL_DELTAS.get(label, 0.0) * min(int(amount), 3)
        tags: list[str] = []
        for identifier in getattr(paper, "key_candidates", lambda: [])():
            tags.extend(self.data.get("catalog", {}).get(identifier, {}).get("tags", []))
        tags.extend(_paper_tags(paper))
        for tag in set(tags):
            entry = self.data.get("preferences", {}).get(tag, {})
            for label, amount in entry.items():
                delta += LABEL_DELTAS.get(label, 0.0) * min(int(amount), 3)
        # A bounded additive nudge preserves exploration and cannot hard-filter a paper.
        return max(-6.0, min(6.0, delta))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
