#!/usr/bin/env python3
"""Restore known TSV headers in preserved, headerless S15 artifacts."""
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s15_remaining_census as current

SCAN_ROOT = Path("remote_input/scan_stage")
OLD_ROOT = Path("remote_input/previous_census")

OLD_CENSUS_FIELDS = (
    "pipeline", "archive", "cancer", "S15_detected", "site_mapping_confidence", "peptide",
    "data_representation", "imputed_or_observed", "total_columns", "reference_columns",
    "nonmissing_columns", "nonreference_nonmissing", "exact_mapped_samples", "unique_patients",
    "primary_tumor_patients", "adjacent_normal_patients", "technical_replicates", "mapping_status",
)


def locate(root, name):
    matches = list(root.rglob(name)) if root.exists() else []
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {name} under {root}; found {len(matches)}")
    return matches[0]


def normalize_tsv(path, fields):
    expected = list(fields)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        table = list(csv.reader(handle, delimiter="\t"))
    if not table:
        raise RuntimeError(f"Refusing to normalize an empty table: {path}")
    first = [cell.strip() for cell in table[0]]
    if first == expected:
        rows = table[1:]
        mode = "header_present"
    else:
        if len(first) != len(expected):
            raise RuntimeError(
                f"Unexpected field count in {path}: first row has {len(first)}, expected {len(expected)}"
            )
        rows = table
        mode = "header_added"
    if not rows:
        raise RuntimeError(f"Refusing to normalize a table with no data rows: {path}")
    for index, row in enumerate(rows, start=1):
        if len(row) != len(expected):
            raise RuntimeError(
                f"Unexpected field count in {path} data row {index}: {len(row)}, expected {len(expected)}"
            )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(expected)
        writer.writerows(rows)
    print(f"NORMALIZED={path}; MODE={mode}; DATA_ROWS={len(rows)}", flush=True)


def main():
    scan_tables = (
        ("S15_CrossPipeline_Cohort_Census.tsv", current.CENSUS_FIELDS),
        ("S15_CrossPipeline_SampleLevel.tsv", current.SAMPLE_FIELDS),
        ("S15_download_audit.tsv", current.DOWNLOAD_FIELDS),
        ("S15_imputation_evidence.tsv", current.EVIDENCE_FIELDS),
    )
    old_tables = (
        ("S15_CrossPipeline_Cohort_Census.tsv", OLD_CENSUS_FIELDS),
        ("S15_download_audit.tsv", current.DOWNLOAD_FIELDS),
        ("S15_imputation_evidence.tsv", current.EVIDENCE_FIELDS),
    )
    for name, fields in scan_tables:
        normalize_tsv(locate(SCAN_ROOT, name), fields)
    for name, fields in old_tables:
        normalize_tsv(locate(OLD_ROOT, name), fields)


if __name__ == "__main__":
    main()
