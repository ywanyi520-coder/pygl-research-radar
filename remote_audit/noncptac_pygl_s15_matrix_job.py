#!/usr/bin/env python3
"""Per-accession worker and final artifact merger for the PYGL S15 audit."""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any

import noncptac_pygl_s15_clinical_audit as audit

STRICT_NAME = re.compile(r"(?:phosphosite|phosphopeptide|phospho\(sty\)sites|site_quant)", re.I)
TABLE_SUFFIXES = (".txt", ".tsv", ".csv", ".xlsx", ".xls", ".txt.gz", ".tsv.gz", ".csv.gz", ".zip")


def strict_downloads(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = []
    for item in files:
        name, category, _ = audit.file_fields(item)
        lower = name.lower()
        if not name or category in {"RAW", "PEAK", "FASTA"}:
            continue
        if lower.endswith(audit.RAW_EXTENSIONS) or not lower.endswith(TABLE_SUFFIXES):
            continue
        if STRICT_NAME.search(name):
            eligible.append(item)
    return eligible


def rows_from_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def per_accession_paths(accession: str) -> tuple[Path, Path, Path, Path]:
    folder = audit.RESULTS / "per_accession" / accession
    return (folder / "NonCPTAC_S15_dataset_inventory.tsv",
            folder / "NonCPTAC_S15_file_manifest.tsv",
            folder / "NonCPTAC_S15_candidate_evidence.tsv",
            folder / "NonCPTAC_S15_audit_status.txt")


def run_accession(study: dict[str, str]) -> int:
    """List the complete manifest first, then download strict-name matches only."""
    pxd = study["accession"]
    inventory_path, manifest_path, evidence_path, status_path = per_accession_paths(pxd)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from pridepy.download.client import Client
        client = Client()
        native, source, files, file_error = audit.list_processed_files(client, pxd)
    except Exception as exc:
        native, source, files = pxd, "unresolved", []
        file_error = f"{type(exc).__name__}: {exc}"

    manifest: list[dict[str, Any]] = []
    for item in files:
        name, category, size = audit.file_fields(item)
        manifest.append({"dataset": pxd, "repository_accession": native,
                         "filename": name, "category": category,
                         "size_bytes": size, "download_status": "MANIFEST_ONLY"})

    # Preserve Phase 1 as a manifest-only artifact before any network transfer.
    audit.write_tsv(manifest_path, audit.MANIFEST_FIELDS, manifest)
    selected = strict_downloads(files)
    selected_names = {audit.file_fields(item)[0] for item in selected}
    for row in manifest:
        if row["category"] == "RAW" or str(row["filename"]).lower().endswith(audit.RAW_EXTENSIONS):
            row["download_status"] = "NOT_DOWNLOADED_RAW"
        elif row["filename"] not in selected_names:
            row["download_status"] = "NOT_DOWNLOADED_FILENAME_FILTER"
        else:
            row["download_status"] = "MATCHED_STRICT_PHOSPHO_FILENAME"

    candidates: list[dict[str, Any]] = []
    scan_note = file_error
    if not file_error and selected:
        try:
            paths, outcomes = audit.download_selected(client, native, pxd, selected)
            outcome_by_name = {item["filename"]: item["download_status"] for item in outcomes}
            for row in manifest:
                if row["filename"] in outcome_by_name:
                    row["download_status"] = outcome_by_name[row["filename"]]
            candidates = audit.scan_dataset(pxd, native, paths)
            scan_note = ""
        except Exception as exc:
            scan_note = f"{type(exc).__name__}: {exc}"
    elif not selected and not file_error:
        scan_note = "No filename matched the strict phosphosite/phosphopeptide allowlist"

    audit.write_tsv(manifest_path, audit.MANIFEST_FIELDS, manifest)
    inventory = audit.classify_dataset(study, files, file_error if not files else "", candidates, scanned=True)
    inventory["processed_phosphosite_available"] = "UNKNOWN" if file_error and not files else ("YES" if selected else "NO")
    inventory["raw_only"] = "YES" if files and not selected and all(audit.file_fields(item)[1] == "RAW" for item in files) else "NO"
    if scan_note and not candidates:
        inventory["status"] = "DATA_NOT_ACCESSIBLE"
    audit.write_tsv(inventory_path, audit.INVENTORY_FIELDS, [inventory])
    audit.write_tsv(evidence_path, audit.EVIDENCE_FIELDS, candidates)
    return finish_worker(pxd, native, source, manifest, selected, candidates, inventory, status_path, scan_note)


def finish_worker(pxd: str, native: str, source: str, manifest: list[dict[str, Any]],
                  selected: list[dict[str, Any]], candidates: list[dict[str, Any]],
                  inventory: dict[str, str], status_path: Path, scan_note: str) -> int:
    lines = [
        f"DATASET={pxd}", f"REPOSITORY_ACCESSION={native}", f"REPOSITORY_RESOLUTION={source}",
        f"MANIFEST_FILE_COUNT={len(manifest)}", f"STRICT_FILENAME_MATCH_COUNT={len(selected)}",
        f"DOWNLOADED_FILE_COUNT={sum(1 for row in manifest if row['download_status'] == 'DOWNLOADED')}",
        f"PYGL_EVIDENCE_ROWS={sum(1 for row in candidates if row['protein_identifiers'])}",
        f"SEQUENCE_CONFIRMED_S15_ROWS={sum(1 for row in candidates if row['sequence_confirmed'] == 'YES')}",
        f"FINAL_STATUS={inventory['status']}", "RAW_DOWNLOADS=0",
    ]
    if scan_note:
        lines.append(f"SCAN_NOTE={scan_note}")
    status_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


def merge_artifacts(input_dir: Path) -> int:
    inventory_rows: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    for path in input_dir.rglob("NonCPTAC_S15_dataset_inventory.tsv"):
        inventory_rows.extend(rows_from_tsv(path))
    for path in input_dir.rglob("NonCPTAC_S15_file_manifest.tsv"):
        manifest_rows.extend(rows_from_tsv(path))
    for path in input_dir.rglob("NonCPTAC_S15_candidate_evidence.tsv"):
        evidence_rows.extend(rows_from_tsv(path))

    observed = {row.get("dataset", "") for row in inventory_rows}
    for study in audit.STUDIES:
        if study["accession"] not in observed:
            inventory_rows.append({
                "dataset": study["accession"], "disease": study["disease"],
                "patient_n": study["patient_n"], "sample_n": study["sample_n"],
                "tissue": study["tissue"], "clinical_endpoint": study["clinical_endpoint"],
                "processed_phosphosite_available": "UNKNOWN",
                "total_proteome_available": study["total_proteome_expected"],
                "patient_mapping_available": study["mapping_expected"],
                "raw_only": "UNKNOWN", "status": "DATA_NOT_ACCESSIBLE",
            })
    order = {study["accession"]: index for index, study in enumerate(audit.STUDIES)}
    inventory_rows.sort(key=lambda row: order.get(row.get("dataset", ""), 999))
    manifest_rows.sort(key=lambda row: (row.get("dataset", ""), row.get("filename", "")))
    evidence_rows.sort(key=lambda row: (row.get("dataset", ""), row.get("filename", ""), int(row.get("row_number") or 0)))
    audit.write_tsv(audit.INVENTORY, audit.INVENTORY_FIELDS, inventory_rows)
    audit.write_tsv(audit.MANIFEST, audit.MANIFEST_FIELDS, manifest_rows)
    audit.write_tsv(audit.EVIDENCE, audit.EVIDENCE_FIELDS, evidence_rows)

    pygl = [row for row in evidence_rows if row.get("protein_identifiers")]
    seq_s15 = [row for row in evidence_rows if row.get("sequence_confirmed") == "YES"]
    quant = [row for row in evidence_rows if row.get("hit_type") == "CONFIRMED_S15_PATIENT_QUANT"]
    lines = [
        "AUDIT_SCOPE=9 prespecified non-CPTAC datasets; no CPTAC/PDC sources queried",
        f"DATASET_INVENTORY_ROWS={len(inventory_rows)}",
        f"DATASETS_WITH_PYGL_ROWS={len({row['dataset'] for row in pygl})}",
        f"DATASETS_WITH_SEQUENCE_CONFIRMED_S15={len({row['dataset'] for row in seq_s15})}",
        f"DATASETS_WITH_CONFIRMED_S15_PATIENT_QUANT={len({row['dataset'] for row in quant})}",
        "RAW_DOWNLOADS=0",
    ]
    audit.STATUS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accession", choices=[study["accession"] for study in audit.STUDIES])
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--input-dir", type=Path, default=Path("collected"))
    args = parser.parse_args()
    if args.merge:
        return merge_artifacts(args.input_dir)
    if args.accession:
        study = next(study for study in audit.STUDIES if study["accession"] == args.accession)
        return run_accession(study)
    parser.error("select --accession for a matrix worker or --merge for the final merge")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

