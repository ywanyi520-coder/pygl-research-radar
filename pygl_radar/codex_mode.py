"""Codex-native preparation, OA hydration, and output validation.

This module is deliberately separate from :mod:`pygl_radar.pipeline`.  The
Codex path prepares a deterministic candidate pool and local evidence package;
Codex itself supplies semantic triage and journal-club review.  No LLM client
is imported or constructed here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import load_config
from .dedup import SeenCache, canonical_doi, canonical_pmid, canonical_title, deduplicate_papers
from .feedback import FeedbackState
from .fulltext import FulltextAcquirer, apply_fulltext
from .models import Paper
from .pipeline import _candidate_order, _fixture, _ingest_remote_feedback, _stratified_cap, _within_window
from .scoring import SCORE_FIELDS
from .sources import collect_candidates
from .sources.common import HTTPClient


EVIDENCE_LEVELS = {"FULLTEXT_READ", "ABSTRACT_ONLY"}
FIGURE_EVIDENCE_MODES = {
    "FIGURE_VISUALLY_READ",
    "FIGURE_LEGEND_ONLY",
    "RESULTS_TEXT_ONLY",
    "ABSTRACT_ONLY",
}
DETAILED_TEXT_FIELDS = (
    "background",
    "knowledge_gap",
    "scientific_question",
    "central_hypothesis",
    "study_design",
    "figure_limitations",
    "causal_chain",
    "topic_mapping",
    "supervisor_brief",
)
DETAILED_LIST_FIELDS = (
    "innovations",
    "figure_walkthrough",
    "key_controls_and_rescues",
    "strengths",
    "limitations",
    "actionable_ideas",
    "do_not_overclaim",
)
DETAILED_FIELDS = DETAILED_TEXT_FIELDS + DETAILED_LIST_FIELDS
FIGURE_FIELDS = (
    "figure",
    "question",
    "approach",
    "key_result",
    "logic_role",
    "caveat",
    "evidence_basis",
)
MAPPING_TERMS = (
    "AC-derived soluble signaling",
    "ATP",
    "P2Y2",
    "PLC",
    "Ca2+",
    "sAC",
    "cAMP",
    "PKA",
    "PhK",
    "PYGL",
    "Ser15",
    "glycogenolysis",
    "post-engulfment processing",
    "phagosome",
    "efferosome",
    "lysosome",
    "acidification",
    "fusion",
    "degradation",
    "rescue",
    "bypass",
    "beta2-AR",
    "norepinephrine",
    "injury resolution",
)
RAW_TEXT_KEYS = {
    "fulltext_text",
    "raw_fulltext",
    "fulltext",
    "article_text",
    "raw_text",
    "parsed_text",
    "pdf_text",
    "evidence_text",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FIGURE_RE = re.compile(r"\b(?:fig(?:ure)?\.?\s*)\d+(?:\s*[-–]\s*\d+)?", re.I)
ABSTRACT_GUARDRAILS = (
    FIGURE_RE,
    re.compile(r"\bsample\s*size\b", re.I),
    re.compile(r"\bn\s*=\s*\d+\b", re.I),
    re.compile(r"\b\d+\s+(?:participants?|donors?|animals?|mice|rats|subjects?)\b", re.I),
    re.compile(r"\b(?:dose|dosing)\s*[:=]?\s*\d", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|μg|µg|ug|ng|nM|μM|µM|uM|mM|%)\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?)\b", re.I),
    re.compile(r"\b(?:incubat(?:ed|ion)|western\s+blot|immunoblot|flow\s+cytometry|confocal|transfection)\b", re.I),
)


class CodexModeError(ValueError):
    """Raised for an invalid Codex workspace or publishable review."""


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexModeError(f"cannot read JSON: {path}") from exc


def _config_value(config_or_path: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    if isinstance(config_or_path, dict):
        return config_or_path
    if config_or_path is not None:
        selected = Path(config_or_path).expanduser()
        if not selected.exists() and selected.name == "config.yaml":
            fallback = selected.with_name("config.example.yaml")
            if fallback.exists():
                return load_config(fallback)
    return load_config(config_or_path)


def stable_paper_id(paper: Paper) -> str:
    """Return a stable, source-independent paper identity."""
    doi = canonical_doi(paper.doi)
    if doi:
        return f"doi:{doi}"
    pmid = canonical_pmid(paper.pmid)
    if pmid:
        return f"pmid:{pmid}"
    title = canonical_title(paper.title)
    if title:
        return f"title:{title}"
    digest = hashlib.sha256(str(paper.title).encode("utf-8")).hexdigest()[:20]
    return f"unknown:{digest}"


def _discovery_lane(paper: Paper) -> str:
    return "journal-first" if any(str(source).endswith("-journal") for source in paper.sources) else "topic"


def _safe_http_url(value: Any) -> str | None:
    candidate = str(value or "").strip()
    return candidate if candidate.startswith(("https://", "http://")) else None


def _lawful_oa_urls(paper: Paper) -> list[str]:
    values: list[str] = []
    if paper.pmcid:
        values.extend([
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{paper.pmcid}/",
            f"https://europepmc.org/articles/{paper.pmcid}",
        ])
    for url in [paper.fulltext_source_url, *paper.source_urls]:
        safe = _safe_http_url(url)
        lowered = (safe or "").casefold()
        if safe and any(marker in lowered for marker in ("pmc", "europepmc", "pdf", "oa", "repository", "archive")):
            values.append(safe)
    return list(dict.fromkeys(values))


def candidate_record(paper: Paper) -> dict[str, Any]:
    """Project a Paper into the pre-triage, Codex-readable metadata contract."""
    source_urls = list(dict.fromkeys(str(url) for url in paper.source_urls if _safe_http_url(url)))
    oa_urls = _lawful_oa_urls(paper)
    return {
        "paper_id": stable_paper_id(paper),
        "title": paper.title,
        "journal": paper.journal,
        "publication_date": paper.publication_date,
        "doi": canonical_doi(paper.doi),
        "pmid": canonical_pmid(paper.pmid),
        "pmcid": paper.pmcid,
        "abstract": paper.abstract,
        "authors": list(paper.authors),
        "sources": list(paper.sources),
        "source_urls": source_urls,
        "source_provenance": {
            "sources": list(paper.sources),
            "source_urls": source_urls,
            "discovery_lane": _discovery_lane(paper),
        },
        "publisher_url": _safe_http_url(paper.publisher_url),
        "lawful_oa_urls": oa_urls,
        "oa_urls": oa_urls,
        "discovery_lane": _discovery_lane(paper),
    }


def _profile_digest(profile: dict[str, Any]) -> str:
    canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_codex_instructions(report_date: str, profile: dict[str, Any]) -> str:
    """Create the per-run instruction packet with the exact frozen profile."""
    profile_json = json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True)
    mapping = ", ".join(MAPPING_TERMS)
    patterns = (
        "conditioned medium → fractionation → candidate ligand → blockade/add-back; "
        "inhibitor/blockade → agonist/activation → rescue; "
        "upstream inhibition → downstream bypass / epistasis; "
        "acute phosphorylation → metabolic change → functional phenotype; "
        "binding intact → post-engulfment processing defect; "
        "metabolism → organelle maturation/function; "
        "injury → delayed intervention → resolution"
    )
    return f"""# PYGL Research Radar — Codex-native run {report_date}

This is a local comparison run. Read the full candidate pool in `candidates.json` before semantic triage. Do not call or configure the DeepSeek/OpenAI-compatible API and do not modify production Python, configuration, tests, GitHub Actions, Pages, Issue, or notifier files.

## Frozen PYGL research profile

The JSON below is copied from the same loaded profile used by the production pipeline. Treat it as frozen for this run; do not add terms, remove terms, or change scoring weights.

```json
{profile_json}
```

Profile digest: `{_profile_digest(profile)}`

## Required sequence

1. Triage every paper in `candidates.json`; do not require the string PYGL to occur in a paper.
2. Weight experimental logic more strongly than simple keyword overlap. Reusable patterns include:
   - {patterns}
3. Keep an auditable shortlist of approximately 10–15 papers in `shortlist.json`. Every entry must use a `paper_id` already present in `candidates.json` and include a short triage rationale. Do not invent papers.
4. Run `python -m pygl_radar codex-hydrate --workspace work/{report_date} --shortlist work/{report_date}/shortlist.json`. Read only the evidence files created there. `FULLTEXT_READ` means lawful OA text was actually fetched and parsed; otherwise use `ABSTRACT_ONLY`.
5. Write `work/{report_date}/codex_reviewed.json` with only the final Top 3–5 papers, using the six 0–100 fields `direct_relevance`, `mechanism_relevance`, `experimental_similarity`, `transferability`, `idea_value`, and `evidence_quality`, plus the detailed journal-club fields listed below.
6. Run `python -m pygl_radar codex-validate --workspace work/{report_date}`. Only a successful validation creates sanitized `codex-output/{report_date}.json` and `.md`.

## Detailed review contract

Each reviewed paper must contain `paper_id`, metadata consistent with the candidate, `evidence_level`, `figure_evidence_mode`, `scores`, `final_score`, and a `review` object with:

`background`, `knowledge_gap`, `scientific_question`, `central_hypothesis`, `study_design`, `innovations`, `figure_walkthrough`, `figure_limitations`, `key_controls_and_rescues`, `causal_chain`, `strengths`, `limitations`, `topic_mapping`, `actionable_ideas`, `do_not_overclaim`, `supervisor_brief`.

Each `figure_walkthrough` item must contain `figure`, `question`, `approach`, `key_result`, `logic_role`, `caveat`, and `evidence_basis`. Use only one of `FIGURE_VISUALLY_READ`, `FIGURE_LEGEND_ONLY`, `RESULTS_TEXT_ONLY`, or `ABSTRACT_ONLY`. Use `FIGURE_VISUALLY_READ` only when an image is genuinely available locally and was actually inspected; otherwise never claim visual inspection.

`topic_mapping` must explicitly discuss applicability and non-applicability to the macrophage PYGL/efferocytosis project, using evidence-bounded language around: {mapping}. Never claim that an outside paper proves a mechanism in the user's system.

## Evidence guardrails

- `ABSTRACT_ONLY` cannot contain figure-number interpretation, dose, sample size, detailed protocol, or a detailed rescue result not present in the abstract. Mark missing support as `NOT_EVALUABLE`.
- `FULLTEXT_READ` is text/legend evidence only unless a local image was actually inspected. Do not infer Western blot intensity, microscopy morphology, scatter distributions, or other visual details from prose.
- Do not copy raw full text, PDF bytes, or evidence text into `codex_reviewed.json` or any `codex-output` file. Keep parsed evidence only under `work/{report_date}/evidence/`.
- On retrieval, validation, or program error, stop and report the failure. Do not auto-patch production source.

The scheduled run may write only `work/` and `codex-output/`. `work/` is gitignored; the sanitized comparison output is the only intended committed artifact.
"""


def prepare_codex_workspace(
    config_or_path: dict[str, Any] | str | Path | None = None,
    *,
    now: datetime | None = None,
    fixture_path: str | Path | None = None,
    workspace_root: str | Path = "work",
    http: HTTPClient | None = None,
) -> dict[str, Any]:
    """Prepare deterministic candidates without instantiating an LLM client."""
    config = _config_value(config_or_path)
    now = now or datetime.now(timezone.utc)
    http = http or HTTPClient(timeout=float(config.get("fulltext", {}).get("timeout_seconds", 30)))
    if fixture_path:
        candidates = _fixture(fixture_path)
        source_stats: dict[str, Any] = {"retrieved": {"fixture": len(candidates)}, "failures": {}}
    else:
        candidates, source_stats = collect_candidates(config, now=now, http=http)
    window_hours = int(config.get("sources", {}).get("window_hours", 48))
    candidates = [paper for paper in candidates if _within_window(paper, now, window_hours)]
    unique = deduplicate_papers(_candidate_order(candidates, config))
    unique = _stratified_cap(unique, config)
    seen = SeenCache(_state_path(config, "seen_cache", "state/seen.json"))
    unseen = [paper for paper in unique if not seen.contains(paper)]
    feedback = FeedbackState(_state_path(config, "feedback", "state/feedback.json"))
    feedback_ingested = _ingest_remote_feedback(feedback)
    records = [candidate_record(paper) for paper in unseen]
    report_date = now.date().isoformat()
    workspace = Path(workspace_root) / report_date
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "evidence").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "date": report_date,
        "profile": config.get("profile", {}),
        "profile_digest": _profile_digest(config.get("profile", {})),
        "candidate_ids": [record["paper_id"] for record in records],
        "candidate_count": len(records),
        "counts": {
            "retrieved_after_window": len(candidates),
            "deduplicated": len(unique),
            "excluded_seen": len(unique) - len(unseen),
            "candidate_pool": len(records),
            "feedback_ingested": feedback_ingested,
        },
        "source_stats": source_stats,
        "hydration": {"paper_ids": [], "evidence_files": {}, "evidence_levels": {}},
    }
    _write_json(workspace / "manifest.json", manifest)
    _write_json(workspace / "candidates.json", {
        "schema_version": 1,
        "date": report_date,
        "profile_digest": manifest["profile_digest"],
        "papers": records,
    })
    (workspace / "codex_instructions.md").write_text(build_codex_instructions(report_date, config.get("profile", {})), encoding="utf-8")
    return {
        "workspace": workspace,
        "manifest": workspace / "manifest.json",
        "candidates": workspace / "candidates.json",
        "codex_instructions": workspace / "codex_instructions.md",
        "candidate_ids": manifest["candidate_ids"],
        "candidate_count": len(records),
    }


def _state_path(config: dict[str, Any], key: str, default: str) -> Path:
    raw = Path(str(config.get("state", {}).get(key, default))).expanduser()
    config_path = config.get("_path")
    if not raw.is_absolute() and config_path:
        raw = Path(config_path).parent / raw
    return raw


def _workspace_manifest(workspace: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(workspace)
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or not DATE_RE.fullmatch(str(manifest.get("date", ""))):
        raise CodexModeError(f"invalid workspace manifest: {manifest_path}")
    return root, manifest


def _candidate_map(workspace: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json(workspace / "candidates.json")
    items = raw.get("papers") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise CodexModeError("candidates.json must contain a papers list")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not str(item.get("paper_id", "")).strip():
            raise CodexModeError("every candidate must have a paper_id")
        paper_id = str(item["paper_id"])
        if paper_id in result:
            raise CodexModeError(f"duplicate candidate paper_id: {paper_id}")
        result[paper_id] = item
    return result


def _shortlist_ids(shortlist_path: str | Path, candidate_ids: set[str], expected_date: str) -> list[str]:
    raw = _read_json(Path(shortlist_path))
    if isinstance(raw, dict):
        if raw.get("date") and str(raw["date"]) != expected_date:
            raise CodexModeError("shortlist date does not match workspace date")
        items = raw.get("papers", raw.get("paper_ids", []))
    else:
        items = raw
    if not isinstance(items, list):
        raise CodexModeError("shortlist.json must contain a list or papers list")
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            paper_id = item.get("paper_id") or item.get("stable_paper_id") or item.get("id")
        else:
            paper_id = item
        paper_id = str(paper_id or "").strip()
        if not paper_id:
            raise CodexModeError("shortlist contains an empty paper_id")
        if paper_id not in candidate_ids:
            raise CodexModeError(f"shortlist references unknown paper_id: {paper_id}")
        if paper_id in ids:
            raise CodexModeError(f"shortlist repeats paper_id: {paper_id}")
        ids.append(paper_id)
    return ids


def _paper_from_candidate(record: dict[str, Any]) -> Paper:
    provenance = record.get("source_provenance") if isinstance(record.get("source_provenance"), dict) else {}
    return Paper(
        title=str(record.get("title", "")),
        abstract=str(record.get("abstract", "")),
        journal=str(record.get("journal", "")),
        publication_date=record.get("publication_date"),
        doi=record.get("doi"),
        pmid=record.get("pmid"),
        pmcid=record.get("pmcid"),
        publisher_url=record.get("publisher_url"),
        source_urls=list(provenance.get("source_urls", record.get("source_urls", [])) or []),
        sources=list(provenance.get("sources", record.get("sources", [])) or []),
        authors=list(record.get("authors", []) or []),
    )


def _evidence_filename(paper_id: str) -> str:
    return hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:24] + ".json"


def hydrate_codex_workspace(
    workspace: str | Path,
    shortlist: str | Path,
    *,
    config_or_path: dict[str, Any] | str | Path | None = None,
    http: HTTPClient | None = None,
    acquirer: FulltextAcquirer | None = None,
) -> dict[str, Any]:
    """Hydrate shortlisted papers through the existing lawful OA chain."""
    root, manifest = _workspace_manifest(workspace)
    candidates = _candidate_map(root)
    shortlist_ids = _shortlist_ids(shortlist, set(candidates), str(manifest["date"]))
    config = _config_value(config_or_path)
    if acquirer is None:
        http = http or HTTPClient(timeout=float(config.get("fulltext", {}).get("timeout_seconds", 30)))
        acquirer = FulltextAcquirer(
            http=http,
            timeout=float(config.get("fulltext", {}).get("timeout_seconds", 30)),
            min_text_chars=int(config.get("fulltext", {}).get("min_text_chars", 200)),
            min_html_text_chars=int(config.get("fulltext", {}).get("min_html_text_chars", 2000)),
        )
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    levels: dict[str, str] = {}
    files: dict[str, str] = {}
    email = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    for paper_id in shortlist_ids:
        paper = _paper_from_candidate(candidates[paper_id])
        result = apply_fulltext(paper, acquirer, unpaywall_email=email)
        if result.evidence_level not in EVIDENCE_LEVELS:
            raise CodexModeError(f"unexpected evidence level for {paper_id}: {result.evidence_level}")
        filename = _evidence_filename(paper_id)
        evidence = {
            "schema_version": 1,
            "paper_id": paper_id,
            "evidence_level": paper.evidence_level,
            "source_url": result.source_url,
            "retrieval_mode": result.retrieval_mode,
            "reason": result.reason,
            "abstract": paper.abstract,
            "lawful_oa_urls": candidates[paper_id].get("lawful_oa_urls", []),
            # This is intentionally local-only under work/evidence. It is
            # stripped by the publishable-output whitelist.
            "parsed_text": paper.fulltext_text if paper.evidence_level == "FULLTEXT_READ" else "",
        }
        _write_json(evidence_dir / filename, evidence)
        levels[paper_id] = paper.evidence_level
        files[paper_id] = f"evidence/{filename}"
    manifest["hydration"] = {
        "paper_ids": shortlist_ids,
        "evidence_files": files,
        "evidence_levels": levels,
    }
    _write_json(root / "manifest.json", manifest)
    return {"workspace": root, "paper_ids": shortlist_ids, "evidence_levels": levels, "evidence_files": files}


def _reviewed_papers(raw: Any, expected_date: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise CodexModeError("codex_reviewed.json must be an object")
    if str(raw.get("date", expected_date)) != expected_date:
        raise CodexModeError("codex_reviewed.json date does not match workspace")
    papers = raw.get("papers")
    if not isinstance(papers, list):
        raise CodexModeError("codex_reviewed.json must contain a papers list")
    return [item for item in papers if isinstance(item, dict)]


def _finite_score(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise CodexModeError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CodexModeError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise CodexModeError(f"{label} must be between 0 and 100")
    return number


def _profile_vocab(profile: dict[str, Any]) -> tuple[set[str], set[str]]:
    mechanisms = {str(item).strip().casefold() for item in profile.get("mechanisms", []) if str(item).strip()}
    patterns = {str(item).strip().casefold() for item in profile.get("experimental_patterns", []) if str(item).strip()}
    return mechanisms, patterns


def _validate_controlled_vocab(paper: dict[str, Any], profile: dict[str, Any]) -> None:
    mechanisms, patterns = _profile_vocab(profile)
    review = paper.get("review") if isinstance(paper.get("review"), dict) else {}
    for field, allowed in (("matched_mechanisms", mechanisms), ("matched_patterns", patterns)):
        values = paper.get(field, review.get(field))
        if values is None:
            continue
        if not isinstance(values, list):
            raise CodexModeError(f"{field} must be a list")
        unknown = [str(item) for item in values if str(item).strip().casefold() not in allowed]
        if unknown:
            raise CodexModeError(f"unknown {field}: {unknown[0]}")
    topic_mapping = str(review.get("topic_mapping", paper.get("topic_mapping", "")))
    lowered = topic_mapping.casefold()
    if "not_evaluable" not in lowered and not any(term.casefold() in lowered for term in MAPPING_TERMS):
        raise CodexModeError("topic_mapping must use at least one controlled PYGL mapping term or NOT_EVALUABLE")


def _all_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() not in RAW_TEXT_KEYS:
                yield from _all_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_text(item)


def _validate_abstract_guardrails(review: dict[str, Any], abstract: str) -> None:
    text = "\n".join(_all_text(review))
    for pattern in ABSTRACT_GUARDRAILS:
        if pattern.search(text):
            raise CodexModeError("ABSTRACT_ONLY contains unsupported figure/dose/sample/protocol detail")
    if "rescue" in text.casefold() and "rescue" not in abstract.casefold():
        descriptive_fields = {key: review.get(key) for key in ("background", "knowledge_gap", "scientific_question", "central_hypothesis", "study_design", "causal_chain", "strengths", "limitations", "supervisor_brief")}
        if "rescue" in "\n".join(_all_text(descriptive_fields)).casefold():
            raise CodexModeError("ABSTRACT_ONLY contains a rescue claim absent from the abstract")


def _validate_figure_walkthrough(review: dict[str, Any], evidence_level: str, mode: str, workspace: Path) -> None:
    figures = review.get("figure_walkthrough")
    if not isinstance(figures, list):
        raise CodexModeError("figure_walkthrough must be a list")
    if mode not in FIGURE_EVIDENCE_MODES:
        raise CodexModeError(f"invalid figure_evidence_mode: {mode}")
    if evidence_level == "ABSTRACT_ONLY":
        if mode != "ABSTRACT_ONLY" or figures:
            raise CodexModeError("ABSTRACT_ONLY cannot contain figure interpretation")
        if "not_evaluable" not in str(review.get("figure_limitations", "")).casefold():
            raise CodexModeError("ABSTRACT_ONLY figure limitations must say NOT_EVALUABLE")
        return
    if mode == "ABSTRACT_ONLY":
        raise CodexModeError("FULLTEXT_READ cannot use ABSTRACT_ONLY figure mode")
    if mode == "FIGURE_VISUALLY_READ":
        paths = review.get("figure_image_paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(item, str) for item in paths):
            raise CodexModeError("FIGURE_VISUALLY_READ requires local figure_image_paths")
        for item in paths:
            image_path = (workspace / item).resolve()
            if workspace.resolve() not in image_path.parents:
                raise CodexModeError("figure image path escapes workspace")
            if not image_path.is_file():
                raise CodexModeError(f"figure image not found: {item}")
    for item in figures:
        if not isinstance(item, dict) or any(not str(item.get(field, "")).strip() for field in FIGURE_FIELDS):
            raise CodexModeError("each figure walkthrough requires all evidence fields")
        if not FIGURE_RE.search(str(item.get("figure"))):
            raise CodexModeError("figure walkthrough has an invalid figure identifier")


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _public_value(item) for key, item in value.items() if str(key).casefold() not in RAW_TEXT_KEYS}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def _sanitized_paper(paper: dict[str, Any], candidate: dict[str, Any], evidence_level: str) -> dict[str, Any]:
    review = paper.get("review") if isinstance(paper.get("review"), dict) else {}
    scores = paper.get("scores") if isinstance(paper.get("scores"), dict) else {}
    metadata_fields = (
        "paper_id", "title", "journal", "publication_date", "doi", "pmid", "pmcid",
        "authors", "publisher_url", "lawful_oa_urls", "discovery_lane",
    )
    result = {field: _public_value(paper.get(field, candidate.get(field))) for field in metadata_fields}
    result["evidence_level"] = evidence_level
    result["figure_evidence_mode"] = str(paper.get("figure_evidence_mode", review.get("figure_evidence_mode", "")))
    result["scores"] = {field: float(scores[field]) for field in SCORE_FIELDS}
    result["final_score"] = float(paper["final_score"])
    result["review"] = {field: _public_value(review.get(field)) for field in DETAILED_FIELDS}
    for optional in ("matched_mechanisms", "matched_patterns"):
        values = paper.get(optional, review.get(optional))
        if values is not None:
            result[optional] = _public_value(values)
    return result


def _render_codex_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# PYGL Research Radar Codex-native — {report['date']}",
        "",
        "> review_provider: `codex-automation` · review_mode: `codex-native`",
        f"> 最终推荐 {len(report['papers'])} 篇；本文件只含 sanitized digest，不含原始全文。",
        "",
    ]
    for index, paper in enumerate(report["papers"], 1):
        review = paper["review"]
        lines.extend([
            f"## {index}. {paper['title']}",
            f"- {paper.get('journal') or '期刊未提供'} · {paper.get('publication_date') or '日期未提供'} · evidence: **{paper['evidence_level']}**",
            f"- final score: **{paper['final_score']:.2f}**",
            "- sub-scores: " + ", ".join(f"{field}={paper['scores'][field]:.1f}" for field in SCORE_FIELDS),
            f"- figure evidence mode: **{paper['figure_evidence_mode']}**",
            "- links: " + " · ".join(link for link in [
                f"[DOI](https://doi.org/{paper['doi']})" if paper.get("doi") else "",
                f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/)" if paper.get("pmid") else "",
                f"[Publisher]({paper['publisher_url']})" if paper.get("publisher_url") else "",
                *[f"[OA]({url})" for url in paper.get("lawful_oa_urls", [])],
            ] if link),
            "",
        ])
        for field in DETAILED_TEXT_FIELDS:
            lines.extend([f"### {field}", str(review.get(field) or "NOT_EVALUABLE"), ""])
        for field in DETAILED_LIST_FIELDS:
            lines.append(f"### {field}")
            values = review.get(field) or []
            if not isinstance(values, list) or not values:
                lines.append("- NOT_EVALUABLE")
            else:
                for value in values:
                    if isinstance(value, dict):
                        lines.append("- " + "; ".join(f"{key}: {item}" for key, item in value.items()))
                    else:
                        lines.append(f"- {value}")
            lines.append("")
        lines.append("---\n")
    return "\n".join(lines)


def validate_codex_workspace(
    workspace: str | Path,
    *,
    output_root: str | Path = "codex-output",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate Codex review evidence and emit only a sanitized comparison report."""
    root, manifest = _workspace_manifest(workspace)
    candidates = _candidate_map(root)
    candidate_ids = set(candidates)
    declared_ids = set(str(item) for item in manifest.get("candidate_ids", []))
    if declared_ids != candidate_ids:
        raise CodexModeError("manifest candidate_ids do not match candidates.json")
    shortlist_path = root / "shortlist.json"
    shortlist_ids = _shortlist_ids(shortlist_path, candidate_ids, str(manifest["date"]))
    raw_reviewed = _read_json(root / "codex_reviewed.json")
    reviewed = _reviewed_papers(raw_reviewed, str(manifest["date"]))
    if len(reviewed) > 5:
        raise CodexModeError("codex_reviewed.json may contain at most 5 final papers")
    hydrated = manifest.get("hydration") if isinstance(manifest.get("hydration"), dict) else {}
    evidence_files = hydrated.get("evidence_files") if isinstance(hydrated.get("evidence_files"), dict) else {}
    evidence_levels = hydrated.get("evidence_levels") if isinstance(hydrated.get("evidence_levels"), dict) else {}
    profile = profile or (manifest.get("profile") if isinstance(manifest.get("profile"), dict) else {})
    sanitized_papers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for paper in reviewed:
        paper_id = str(paper.get("paper_id", "")).strip()
        if paper_id not in candidate_ids:
            raise CodexModeError(f"review references unknown paper_id: {paper_id}")
        if paper_id not in shortlist_ids:
            raise CodexModeError(f"review paper is not in shortlist: {paper_id}")
        if paper_id in seen_ids:
            raise CodexModeError(f"review repeats paper_id: {paper_id}")
        seen_ids.add(paper_id)
        candidate = candidates[paper_id]
        if paper.get("title") and str(paper["title"]).strip() != str(candidate.get("title", "")).strip():
            raise CodexModeError(f"review title does not match candidate: {paper_id}")
        evidence_file = evidence_files.get(paper_id)
        if not isinstance(evidence_file, str):
            raise CodexModeError(f"missing evidence file for: {paper_id}")
        evidence_path = (root / evidence_file).resolve()
        if root.resolve() not in evidence_path.parents or not evidence_path.is_file():
            raise CodexModeError(f"invalid evidence file for: {paper_id}")
        evidence = _read_json(evidence_path)
        if not isinstance(evidence, dict) or evidence.get("paper_id") != paper_id:
            raise CodexModeError(f"evidence provenance mismatch: {paper_id}")
        evidence_level = str(paper.get("evidence_level", ""))
        if evidence_level not in EVIDENCE_LEVELS or evidence_level != str(evidence.get("evidence_level")) or evidence_level != str(evidence_levels.get(paper_id)):
            raise CodexModeError(f"evidence level mismatch: {paper_id}")
        if evidence_level == "FULLTEXT_READ" and (not str(evidence.get("source_url", "")).startswith(("https://", "http://")) or not str(evidence.get("parsed_text", "")).strip()):
            raise CodexModeError(f"FULLTEXT_READ lacks parsed OA evidence: {paper_id}")
        scores = paper.get("scores")
        if not isinstance(scores, dict):
            raise CodexModeError(f"scores missing for: {paper_id}")
        for field in SCORE_FIELDS:
            _finite_score(scores.get(field), label=f"{paper_id}.{field}")
        final_score = _finite_score(paper.get("final_score"), label=f"{paper_id}.final_score")
        review = paper.get("review")
        if not isinstance(review, dict):
            raise CodexModeError(f"review missing for: {paper_id}")
        for field in DETAILED_TEXT_FIELDS:
            if not isinstance(review.get(field), str):
                raise CodexModeError(f"review.{field} must be a string: {paper_id}")
        for field in DETAILED_LIST_FIELDS:
            if not isinstance(review.get(field), list):
                raise CodexModeError(f"review.{field} must be a list: {paper_id}")
        mode = str(paper.get("figure_evidence_mode", review.get("figure_evidence_mode", "")))
        _validate_figure_walkthrough(review, evidence_level, mode, root)
        if evidence_level == "ABSTRACT_ONLY":
            _validate_abstract_guardrails(review, str(candidate.get("abstract", "")))
        _validate_controlled_vocab(paper, profile)
        sanitized_papers.append(_sanitized_paper(paper, candidate, evidence_level) | {"final_score": final_score})
    output = {
        "version": 1,
        "date": manifest["date"],
        "review_provider": "codex-automation",
        "review_mode": "codex-native",
        "profile_digest": manifest.get("profile_digest", ""),
        "stats": {
            "candidate_pool": len(candidates),
            "shortlist": len(shortlist_ids),
            "recommended": len(sanitized_papers),
            "evidence": {
                "FULLTEXT_READ": sum(paper["evidence_level"] == "FULLTEXT_READ" for paper in sanitized_papers),
                "ABSTRACT_ONLY": sum(paper["evidence_level"] == "ABSTRACT_ONLY" for paper in sanitized_papers),
            },
        },
        "papers": sanitized_papers,
    }
    output_path = Path(output_root)
    _write_json(output_path / f"{manifest['date']}.json", output)
    (output_path / f"{manifest['date']}.md").write_text(_render_codex_markdown(output), encoding="utf-8")
    return {"valid": True, "date": manifest["date"], "papers": len(sanitized_papers), "json": output_path / f"{manifest['date']}.json", "markdown": output_path / f"{manifest['date']}.md"}


__all__ = [
    "CodexModeError",
    "build_codex_instructions",
    "candidate_record",
    "hydrate_codex_workspace",
    "prepare_codex_workspace",
    "stable_paper_id",
    "validate_codex_workspace",
]
