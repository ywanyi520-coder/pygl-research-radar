#!/usr/bin/env python3
"""S15-only census of the four PDC Pan-Cancer phosphoproteome exports."""
import csv
import gzip
import hashlib
import itertools
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_pdc_pancancer as base

OUT = Path("remote_results")
TMP = Path("/tmp/pdc_pygl_s15_remaining")
OUT.mkdir(parents=True, exist_ok=True)
TMP.mkdir(parents=True, exist_ok=True)
ARCHIVES = base.FILES
COHORTS = ("BRCA", "ccRCC", "COAD", "GBM", "HGSC/OV", "HNSCC", "LSCC", "LUAD", "PDAC", "UCEC", "MB")
CANONICAL = "MAKPLTDQEKRRQISIRGIVGV"

CENSUS_FIELDS = (
    "pipeline", "archive", "cancer", "S15_detected", "site_mapping_confidence", "peptide",
    "data_representation", "imputed_or_observed", "total_columns", "reference_columns",
    "nonmissing_columns", "nonreference_nonmissing", "exact_mapped_samples", "unique_patients",
    "primary_tumor_patients", "adjacent_normal_patients", "technical_replicates",
    "observed_patient_n", "imputed_patient_n", "unresolved_value_patient_n", "mapping_status",
)
DOWNLOAD_FIELDS = ("archive", "file_id", "size_bytes", "sha256", "http_status", "parse_status", "matrix_members", "s15_rows", "error")
EVIDENCE_FIELDS = ("pipeline", "archive", "source", "evidence_type", "finding", "status")
SAMPLE_FIELDS = ("pipeline", "archive", "cancer", "archive_member", "representation", "row_number",
                 "row_identifier", "peptide", "modified_peptide", "site_annotation", "matrix_column", "value_text",
                 "is_reference", "numeric_cell", "source_value_status")


def writer(path, fields):
    handle = Path(path).open("w", encoding="utf-8", newline="")
    return handle, csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")


def normalize(value):
    return base.norm(value)


def classify_cohort(text):
    label = base.cohort(text)
    return "HGSC/OV" if label in ("HGSC", "HGSC/OV") else label


def representation(member):
    n = normalize(member)
    if "multisite" in n:
        return "multi-site"
    if "singlesite" in n:
        return "single-site"
    if "peptide" in n:
        return "peptide"
    return "unspecified phosphosite matrix"


def row_field(header, row, tokens, exclude=()):
    return base.field(header, row, tokens, exclude)


def target_identity(header, row, ensp):
    metadata_idx = [i for i, name in enumerate(header)
                    if any(t in normalize(name) for t in base.META_TOKENS)]
    texts = [str(row[i]) for i in metadata_idx if i < len(row)]
    joined = " | ".join(texts).upper()
    id_match = bool(
        re.search(r"(?<![A-Z0-9])ENSG00000100504(?:\.\d+)?(?![A-Z0-9])", joined)
        or re.search(r"(?<![A-Z0-9])P06737(?![A-Z0-9])", joined)
        or re.search(r"(?<![A-Z0-9])PYGL(?![A-Z0-9])", joined)
        or any(re.search(rf"(?<![A-Z0-9]){re.escape(x.upper())}(?![A-Z0-9])", joined) for x in ensp)
    )
    peptide = row_field(header, row, ("peptide",), ("modified",)) or row_field(header, row, ("sequence",))
    modified = row_field(header, row, ("modifiedpeptide", "modifiedsequence", "modpeptide"))
    seqs = [x.strip() for x in (modified or peptide).split(";") if x.strip()]
    motif_match = any("QISIRGIVGV" in base.peptide_sequence(x)[0] for x in seqs)
    return id_match or motif_match, peptide, modified, joined


def parse_matrix(stream, archive_name, member_name, pipeline, seq, ensp, counters):
    header, rows, _, parse_note = base.read_delimited(stream, member_name)
    if not header:
        raise ValueError(parse_note)
    first_iter = iter(rows)
    first = next(first_iter, None)
    if first is None:
        return 0
    first = ["" if v is None else str(v) for v in first]
    _, sample_idx = base.split_columns(header, first)
    hit_n = 0
    for row_number, raw in enumerate(itertools.chain((first,), first_iter), start=1):
        values = ["" if v is None else str(v) for v in raw]
        if not values or all(base.missing(v) for v in values):
            continue
        is_target, peptide, modified, _ = target_identity(header, values, ensp)
        if not is_target:
            continue
        row_identifier = row_field(header, values, ("rowid", "identifier", "feature", "id", "index", "description"))
        protein = row_field(header, values, ("uniprot", "accession", "proteinid", "protein"))
        site = row_field(header, values, ("phosphosite", "site", "position"), ("probability", "localization", "confidence", "score")) or row_identifier
        call, canonical, why = base.classify(site, peptide, modified, seq)
        if call != "CONFIRMED_S15":
            if call == "AMBIGUOUS" and base.could_be_ambiguous_s15(site, peptide, modified, seq):
                counters["ambiguous"] += 1
            continue
        explicit = row_field(header, values, ("cohort", "cancer", "disease", "tumor type"))
        cancer = classify_cohort(explicit) if explicit else classify_cohort(member_name)
        if cancer == "UNKNOWN":
            cancer = "PAN_CANCER"
        ref_idx = [i for i in sample_idx if any(t in normalize(header[i]) for t in ("reference", "pool", "bridge", "refint"))]
        numeric_idx = [i for i in sample_idx if i < len(values) and base.numeric(values[i])]
        nonref_idx = [i for i in numeric_idx if i not in ref_idx]
        nonmissing_ids = [header[i] for i in numeric_idx]
        nonreference_ids = [header[i] for i in nonref_idx]
        key = (pipeline, cancer)
        counters["cohorts"][key]["hits"].append({
            "archive": archive_name, "member": member_name, "row_number": row_number, "row_identifier": row_identifier,
            "protein": protein, "peptide": peptide, "modified": modified, "site": site,
            "mapping_confidence": "HIGH: unique canonical sequence mapping", "mapping_evidence": why,
            "representation": representation(member_name), "total_columns": len(sample_idx),
            "reference_columns": len(ref_idx), "numeric_idx": numeric_idx,
            "nonref_idx": nonref_idx, "nonmissing_ids": nonmissing_ids,
            "samples": [{"matrix_column": header[i], "value_text": values[i],
                         "is_reference": i in ref_idx, "numeric_cell": base.numeric(values[i])}
                        for i in sample_idx if i < len(values)],
        })
        hit_n += 1
    return hit_n


def scan_zip(path, archive_name, pipeline, seq, ensp, counters):
    matrix_members = hit_count = 0
    with zipfile.ZipFile(path) as zf:
        for item in zf.infolist():
            low = item.filename.lower()
            if item.is_dir() or low.startswith("__macosx/") or Path(low).name.startswith("._"):
                continue
            if any(t in low for t in ("readme", "protocol", "normalization", "processing")) and item.file_size < 10_000_000:
                try:
                    content = zf.read(item).decode("utf-8", errors="replace")
                    for line in content.splitlines():
                        if re.search(r"imput|missing value|observed value|median cent|normaliz", line, re.I):
                            counters["evidence"].append({"pipeline": pipeline, "archive": archive_name,
                                "source": item.filename, "evidence_type": "archive documentation keyword",
                                "finding": line[:700], "status": "REVIEW"})
                except Exception as exc:
                    counters["evidence"].append({"pipeline": pipeline, "archive": archive_name,
                        "source": item.filename, "evidence_type": "archive documentation read",
                        "finding": type(exc).__name__, "status": "UNREADABLE"})
            if not low.endswith((".tsv", ".csv", ".txt", ".tsv.gz", ".csv.gz")):
                continue
            if not any(term in low for term in ("phosph", "report_abundance", "multi-site", "single-site")):
                continue
            matrix_members += 1
            try:
                with zf.open(item) as raw:
                    stream = gzip.GzipFile(fileobj=raw, mode="rb") if low.endswith(".gz") else raw
                    hit_count += parse_matrix(stream, archive_name, item.filename, pipeline, seq, ensp, counters)
                    if stream is not raw:
                        stream.close()
            except Exception as exc:
                counters["errors"].append(f"{item.filename}: {type(exc).__name__}: {base.safe_error(exc)}")
    return matrix_members, hit_count


def scan_flat_gzip(path, archive_name, pipeline, seq, ensp, counters):
    """Scan the Broad TSV.GZ as a single matrix instead of treating it as a ZIP."""
    hit_count = 0
    try:
        with gzip.open(path, "rb") as stream:
            hit_count = parse_matrix(stream, archive_name, archive_name, pipeline, seq, ensp, counters)
    except Exception as exc:
        counters["errors"].append(f"{archive_name}: {type(exc).__name__}: {base.safe_error(exc)}")
        raise
    return 1, hit_count


def main():
    for name in ("S15_CrossPipeline_Cohort_Census.tsv", "S15_download_audit.tsv",
                 "S15_imputation_evidence.tsv", "S15_FINAL_STATUS.txt", "S15_remote_audit.log"):
        (OUT / name).unlink(missing_ok=True)
    log_handle, log_writer = writer(OUT / "S15_remote_audit.log", ("event", "details"))
    down_handle, down_writer = writer(OUT / "S15_download_audit.tsv", DOWNLOAD_FIELDS)
    evidence_handle, evidence_writer = writer(OUT / "S15_imputation_evidence.tsv", EVIDENCE_FIELDS)
    sample_handle, sample_writer = writer(OUT / "S15_CrossPipeline_SampleLevel.tsv", SAMPLE_FIELDS)
    sequence, ensp = base.get_reference()
    if "QEKRRQISIRGIVGV" not in sequence and "QISIRGIVGV" not in sequence:
        raise RuntimeError("Retrieved P06737 sequence does not contain the canonical S15 motif")
    counters = {"cohorts": defaultdict(lambda: {"hits": []}), "evidence": [], "errors": [], "ambiguous": 0}
    downloaded = parsed = metadata_ok = 0
    for archive_name in ARCHIVES:
        pipeline = base.pipeline(archive_name)
        records, status, host, http, error = base.query_metadata(archive_name)
        rec = next((r for r in records if r.get("file_name") == archive_name), None)
        if not rec:
            down_writer.writerow({"archive": archive_name, "parse_status": "NOT_RUN", "error": f"{status}; {error}"})
            log_writer.writerow({"event": "metadata failed", "details": f"{archive_name}: {status} {error}"})
            continue
        metadata_ok += 1
        path, ok, size, sha, http, error = base.download(archive_name, rec.get("file_id"))
        if not ok:
            down_writer.writerow({"archive": archive_name, "file_id": rec.get("file_id", ""),
                                  "http_status": http, "parse_status": "NOT_RUN", "error": error})
            continue
        downloaded += 1
        try:
            scanner = scan_zip if archive_name.lower().endswith(".zip") else scan_flat_gzip
            matrix_members, hits = scanner(path, archive_name, pipeline, sequence, ensp,
                                           counters)
            parsed += 1
            down_writer.writerow({"archive": archive_name, "file_id": rec.get("file_id", ""),
                "size_bytes": size, "sha256": sha, "http_status": http, "parse_status": "PARSED",
                "matrix_members": matrix_members, "s15_rows": hits, "error": ""})
            log_writer.writerow({"event": "archive parsed", "details": f"{archive_name} sha256={sha} matrix_members={matrix_members} s15_rows={hits}"})
        except Exception as exc:
            down_writer.writerow({"archive": archive_name, "file_id": rec.get("file_id", ""),
                "size_bytes": size, "sha256": sha, "http_status": http, "parse_status": "PARSE_FAILED",
                "error": f"{type(exc).__name__}: {base.safe_error(exc)}"})
            counters["errors"].append(f"{archive_name}: {type(exc).__name__}: {base.safe_error(exc)}")
        finally:
            path.unlink(missing_ok=True)
    down_handle.close(); log_handle.close()

    # Sample identifiers and values are kept only in the temporary Actions artifact.
    all_pipelines = ("UMich", "BCM", "UMich_Sinai", "Broad")
    census_handle, census_writer = writer(OUT / "S15_CrossPipeline_Cohort_Census.tsv", CENSUS_FIELDS)
    for pipeline in all_pipelines:
        archive_name = next((name for name in base.FILES if base.pipeline(name) == pipeline), "")
        for cancer in COHORTS:
            hits = list(counters["cohorts"].get((pipeline, cancer), {}).get("hits", []))
            if hits:
                h = hits[0]
                ids = sorted({v for hit in hits for v in hit.get("nonmissing_ids", [])})
                nonref = sorted({v for hit in hits for v in hit.get("nonmissing_ids", [])
                                 if not re.search(r"(?i)(refint|pool|bridge)", v)})
                # The same column may appear in several row views; cohort-level counts are unique column IDs here.
                total = h.get("total_columns", "PENDING")
                reference = h.get("reference_columns", "PENDING")
                observed = len(ids)
                nonref_n = len(nonref)
                peptide = ";".join(sorted({hit.get("peptide", "") for hit in hits if hit.get("peptide")}))
                reps = ";".join(sorted({hit.get("representation", "") for hit in hits if hit.get("representation")}))
                source = "NUMERIC_IN_SOURCE_MATRIX; OBSERVED_VS_IMPUTED_STATUS_RECORDED_PER_CELL_IN_TEMPORARY_ARTIFACT"
                mapping_status = "PENDING_EXACT_SAMPLE_TO_CASE_MAPPING"
                exact_samples = "PENDING_EXACT_SAMPLE_TO_CASE_MAPPING"
                unique_patients = primary = adjacent = technical = "PENDING_EXACT_SAMPLE_TO_CASE_MAPPING"
                observed_patient_n = imputed_patient_n = unresolved_patient_n = "PENDING_EXACT_SAMPLE_TO_CASE_MAPPING"
                confidence = "HIGH: sequence-level canonical mapping" if pipeline != "UMich" else "HIGH: frozen prior result"
            else:
                total = reference = observed = nonref_n = 0
                peptide = reps = "NONE"
                source = "NOT_APPLICABLE_NO_S15_ROW"
                mapping_status = "NOT_APPLICABLE_NO_S15_ROW"
                exact_samples = "NONE"
                unique_patients = primary = adjacent = technical = 0
                observed_patient_n = imputed_patient_n = unresolved_patient_n = 0
                confidence = "NONE"
            census_writer.writerow({"pipeline": pipeline, "archive": archive_name, "cancer": cancer,
                "S15_detected": "YES" if hits else "NO", "site_mapping_confidence": confidence,
                "peptide": peptide, "data_representation": reps, "imputed_or_observed": source,
                "total_columns": total, "reference_columns": reference, "nonmissing_columns": observed,
                "nonreference_nonmissing": nonref_n, "exact_mapped_samples": exact_samples,
                "unique_patients": unique_patients, "primary_tumor_patients": primary,
                "adjacent_normal_patients": adjacent, "technical_replicates": technical,
                "observed_patient_n": observed_patient_n, "imputed_patient_n": imputed_patient_n,
                "unresolved_value_patient_n": unresolved_patient_n,
                "mapping_status": mapping_status})
    census_handle.close()
    for (pipeline, cancer), cohort in sorted(counters["cohorts"].items()):
        for hit in cohort["hits"]:
            for sample in hit.get("samples", []):
                sample_writer.writerow({
                    "pipeline": pipeline, "archive": hit.get("archive", ""),
                    "cancer": cancer, "archive_member": hit.get("member", ""),
                    "representation": hit.get("representation", ""), "row_number": hit.get("row_number", ""),
                    "row_identifier": hit.get("row_identifier", ""),
                    "peptide": hit.get("peptide", ""), "modified_peptide": hit.get("modified", ""),
                    "site_annotation": hit.get("site", ""), "matrix_column": sample.get("matrix_column", ""),
                    "value_text": sample.get("value_text", ""), "is_reference": "YES" if sample.get("is_reference") else "NO",
                    "numeric_cell": "YES" if sample.get("numeric_cell") else "NO",
                    "source_value_status": "NUMERIC_VALUE_IN_SOURCE_MATRIX; OBSERVED_VS_IMPUTED_UNRESOLVED" if sample.get("numeric_cell") else "MISSING_IN_SOURCE_MATRIX"})
    sample_handle.close()
    for item in counters["evidence"]:
        evidence_writer.writerow(item)
    evidence_writer.writerow({"pipeline": "UMich", "archive": base.FILES[0], "source": "TMT-Integrator output naming and upstream README",
        "evidence_type": "normalization interpretation", "finding": "protNorm=MD means median-centering normalization; it is not matched total-PYGL residualization.",
        "status": "DOCUMENTED_BY_TMT_INTEGRATOR; verify archived source readmes before cohort freeze"})
    evidence_writer.writerow({"pipeline": "PanCancer study", "archive": "all", "source": "Geffen et al., Cell 2023, STAR Methods",
        "evidence_type": "imputation handling", "finding": "KNN imputation was described for the downstream clustering feature set after filtering. A numeric cell in the source matrix alone does not establish whether it was measured or imputed.",
        "status": "SOURCE_TABLE_VALUE_STATUS_REQUIRES_MATRIX_AND_PIPELINE_README_RECONCILIATION"})
    evidence_handle.close()
    status = [f"METADATA_SUCCESS={metadata_ok}/4", f"DOWNLOAD_SUCCESS={downloaded}/4", f"PARSE_SUCCESS={parsed}/4",
              f"CONFIRMED_S15_ROWS={sum(len(v['hits']) for v in counters['cohorts'].values())}",
              f"AMBIGUOUS_S15_CANDIDATES={counters['ambiguous']}", f"PARSE_ERRORS={len(counters['errors'])}",
              "NEXT_STEP=EXACT_MAPPING_AND_ENDPOINT_AVAILABILITY"]
    status.extend(counters["errors"])
    (OUT / "S15_FINAL_STATUS.txt").write_text("\n".join(status) + "\n", encoding="utf-8")
    print("\n".join(status), flush=True)


if __name__ == "__main__":
    main()
