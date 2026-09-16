from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "profile": {
        "name": "PYGL Research Radar",
        "system": "macrophage efferocytosis / phagosome-lysosome processing",
        "mechanisms": [
            "apoptotic-cell soluble signals", "ATP", "P2Y2", "PLC", "Ca2+",
            "sAC", "cAMP", "PKA", "PhK", "PYGL", "Ser15", "glycogenolysis",
            "phagosome", "lysosome", "acidification", "maturation", "fusion",
            "degradation", "beta2-AR", "norepinephrine", "injury resolution",
        ],
        "experimental_patterns": [
            "conditioned medium", "size fractionation", "candidate ligand",
            "blockade", "add-back", "inhibitor", "agonist", "rescue",
            "epistasis", "downstream bypass", "acute phosphorylation",
            "metabolic change", "functional phenotype", "post-engulfment",
            "organelle function", "delayed intervention", "resolution",
        ],
        "keywords": ["macrophage", "efferocytosis", "phagosome", "lysosome", "glycogen"],
    },
    "sources": {
        "window_hours": 48,
        "max_candidates": 80,
        "pubmed": {"enabled": True, "max_results": 80},
        "crossref": {"enabled": True, "max_results": 80},
        "optional": {"biorxiv": False, "medrxiv": False, "europe_pmc": True},
        "journal_lane": {
            "enabled": True,
            "max_results": 40,
            "exclude_publication_types": ["Editorial", "News", "Comment", "Letter", "Review"],
        },
        "journal_candidate_slots": 40,
        "journal_per_venue_quota": 8,
        "prioritize_journals": [
            "Nature", "Cell", "Science", "Nature Immunology", "Nature Metabolism",
            "Nature Cell Biology", "Nature Medicine", "Nature Communications",
            "Immunity", "Cell Metabolism", "Cell Reports Medicine",
            "Science Immunology", "Science Translational Medicine", "J Exp Med",
            "Proc Natl Acad Sci U S A",
        ],
    },
    "triage": {"batch_size": 20, "retain": 15},
    "fulltext": {"enabled": True, "min_text_chars": 200, "min_html_text_chars": 2000, "timeout_seconds": 30},
    "scoring": {
        "weights": {
            "direct_relevance": 0.12,
            "mechanism_relevance": 0.14,
            "experimental_similarity": 0.24,
            "transferability": 0.22,
            "idea_value": 0.18,
            "evidence_quality": 0.10,
        },
        "min_score": 50,
        "top_n": 5,
    },
    "output": {"directory": "reports"},
    "publishing": {"enabled": True, "public_site_url": ""},
    "state": {"seen_cache": "state/seen.json", "feedback": "state/feedback.json"},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load YAML config and merge it onto safe defaults."""
    selected = Path(path or os.environ.get("PYGL_RADAR_CONFIG", "config.yaml")).expanduser()
    if not selected.exists():
        if path is not None:
            raise FileNotFoundError(f"Config file not found: {selected}")
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise RuntimeError("PyYAML is required to read config YAML") from exc
    raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a YAML mapping")
    config = _deep_merge(DEFAULT_CONFIG, raw)
    config["_path"] = str(selected.resolve())
    return config


def resolve_path(config: dict[str, Any], key: str, *, default: str) -> Path:
    raw = str(config.get("state", {}).get(key, default))
    path = Path(raw).expanduser()
    config_path = config.get("_path")
    if not path.is_absolute() and config_path:
        path = Path(config_path).parent / path
    return path
