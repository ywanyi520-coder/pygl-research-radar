#!/usr/bin/env python3
"""Merge the completed BCM/Sinai census with targeted UMich/Broad recovery outputs."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_pdc_pancancer as base
import s15_remaining_census as scan

OUT = Path("remote_results")
OLD = Path("remote_input/previous_census")
KEEP_OLD = {"BCM", "UMich_Sinai"}
KEEP_NEW = {"UMich", "Broad"}


def read_tsv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path, fields, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t",
                                lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pipeline_for_archive(row):
    return base.pipeline(row.get("archive", ""))


def merge_rows(old_rows, new_rows, keep_old, keep_new, key):
    selected = []
    seen = set()
    for rows, allowed in ((old_rows, keep_old), (new_rows, keep_new)):
        for row in rows:
            pipeline = row.get(key, "") if key == "pipeline" else pipeline_for_archive(row)
            if pipeline not in allowed:
                continue
            identity = tuple(sorted(row.items()))
            if identity not in seen:
                selected.append(row)
                seen.add(identity)
    return selected


def write_with_fields(path, fields, rows):
    normalized = [{field: row.get(field, "") for field in fields} for row in rows]
    write_tsv(path, fields, normalized)


def main():
    print("MERGE_VALIDATION=FOUR_PIPELINE_COVERAGE; COHORT_GAPS_REPORTED_WITHOUT_SYNTHETIC_ROWS", flush=True)
    census_path = OUT / "S15_CrossPipeline_Cohort_Census.tsv"
    previous_census = OLD / "S15_CrossPipeline_Cohort_Census.tsv"
    current_census = read_tsv(census_path)
    old_census = read_tsv(previous_census)
    merged_census = merge_rows(old_census, current_census, KEEP_OLD, KEEP_NEW, "pipeline")
    expected_pipelines = {"UMich", "BCM", "UMich_Sinai", "Broad"}
    actual_pipelines = {row.get("pipeline", "") for row in merged_census}
    if actual_pipelines != expected_pipelines:
        raise RuntimeError("Merged census lacks one or more of the four audited pipelines")
    pipeline_row_counts = {pipeline: sum(row.get("pipeline") == pipeline for row in merged_census)
                           for pipeline in sorted(expected_pipelines)}
    expected_cohort_keys = {(pipeline, cancer) for pipeline in expected_pipelines
                            for cancer in scan.COHORTS}
    actual_cohort_keys = {(row.get("pipeline", ""), row.get("cancer", ""))
                          for row in merged_census}
    missing_cohort_rows = len(expected_cohort_keys - actual_cohort_keys)
    duplicate_cohort_rows = len(merged_census) - len(actual_cohort_keys)
    if duplicate_cohort_rows:
        raise RuntimeError("Merged census contains duplicate pipeline/cohort rows")
    print("MERGED_CENSUS_ROWS=%d; PIPELINE_ROWS=%s; MISSING_COHORT_ROWS=%d" %
          (len(merged_census), pipeline_row_counts, missing_cohort_rows), flush=True)
    write_with_fields(census_path, scan.CENSUS_FIELDS, merged_census)

    download_path = OUT / "S15_download_audit.tsv"
    old_downloads = read_tsv(OLD / "S15_download_audit.tsv")
    new_downloads = read_tsv(download_path)
    merged_downloads = merge_rows(old_downloads, new_downloads, KEEP_OLD, KEEP_NEW, "archive")
    write_with_fields(download_path, scan.DOWNLOAD_FIELDS, merged_downloads)
    status_by_pipeline = {pipeline_for_archive(row): row.get("parse_status", "")
                          for row in merged_downloads}
    required = {"UMich", "BCM", "UMich_Sinai", "Broad"}
    if set(status_by_pipeline) != required or any(
            not value.startswith("PARSED") for value in status_by_pipeline.values()):
        raise RuntimeError("All four source archives must parse before route mapping")

    evidence_path = OUT / "S15_imputation_evidence.tsv"
    old_evidence = read_tsv(OLD / "S15_imputation_evidence.tsv")
    new_evidence = read_tsv(evidence_path)
    merged_evidence = []
    seen_evidence = set()
    for row in old_evidence + new_evidence:
        pipeline = row.get("pipeline", "")
        if pipeline not in KEEP_OLD | KEEP_NEW | {"PanCancer study"}:
            continue
        identity = tuple(row.get(field, "") for field in scan.EVIDENCE_FIELDS)
        if identity not in seen_evidence:
            merged_evidence.append(row)
            seen_evidence.add(identity)
    write_with_fields(evidence_path, scan.EVIDENCE_FIELDS, merged_evidence)

    sample_rows = read_tsv(OUT / "S15_CrossPipeline_SampleLevel.tsv")
    umich_brca = [row for row in sample_rows if row.get("pipeline") == "UMich"
                  and row.get("cancer") == "BRCA" and row.get("numeric_cell") == "YES"]
    if not umich_brca:
        raise RuntimeError("Targeted UMich BRCA scan returned no numeric S15 cells")

    previous_status = (OLD / "S15_FINAL_STATUS.txt").read_text(encoding="utf-8")
    current_status = (OUT / "S15_FINAL_STATUS.txt").read_text(encoding="utf-8")
    def status_value(text, key, fallback="0"):
        for line in text.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return fallback
    confirmed = int(status_value(previous_status, "CONFIRMED_S15_ROWS")) + int(
        status_value(current_status, "CONFIRMED_S15_ROWS"))
    ambiguous = int(status_value(previous_status, "AMBIGUOUS_S15_CANDIDATES")) + int(
        status_value(current_status, "AMBIGUOUS_S15_CANDIDATES"))
    (OUT / "S15_FINAL_STATUS.txt").write_text(
        "METADATA_SUCCESS=4/4\nDOWNLOAD_SUCCESS=4/4\nPARSE_SUCCESS=4/4\n"
        f"CONFIRMED_S15_ROWS={confirmed}\nAMBIGUOUS_S15_CANDIDATES={ambiguous}\n"
        "PARSE_ERRORS=0\nNEXT_STEP=EXACT_MAPPING_AND_ENDPOINT_AVAILABILITY\n",
        encoding="utf-8")
    (OUT / "S15_remote_audit.log").write_text(
        "event\tdetails\n"
        "recovery_merge\tBCM and UMich_Sinai reused from successful census; UMich used frozen-member HTTP Range reads; Broad streamed as TSV.GZ\n",
        encoding="utf-8")
    print((OUT / "S15_FINAL_STATUS.txt").read_text(encoding="utf-8"), flush=True)


if __name__ == "__main__":
    main()
