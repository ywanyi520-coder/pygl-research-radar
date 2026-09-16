from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LABELS = {"relevant", "idea", "method", "skip"}
LABEL_DELTAS = {"relevant": 2.0, "idea": 2.5, "method": 2.0, "skip": -2.0}


def _key(paper_id: str) -> str:
    value = str(paper_id or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


class FeedbackState:
    """Versioned, append-only-enough feedback state with a deliberately soft effect."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": 1, "papers": {}, "events": []}
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
            self.data.setdefault("papers", {})
            self.data.setdefault("events", [])

    def record(self, paper_id: str, label: str, *, timestamp: str | None = None) -> None:
        label = str(label).strip().lower()
        if label not in LABELS:
            raise ValueError(f"label must be one of {sorted(LABELS)}")
        key = _key(paper_id)
        entry = self.data["papers"].setdefault(key, {name: 0 for name in LABELS})
        entry[label] = int(entry.get(label, 0)) + 1
        self.data["events"].append({"paper_id": key, "label": label, "timestamp": timestamp})

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
        # A bounded additive nudge preserves exploration and cannot hard-filter a paper.
        return max(-6.0, min(6.0, delta))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
