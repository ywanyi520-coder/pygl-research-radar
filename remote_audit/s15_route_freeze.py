#!/usr/bin/env python3
"""Map observed source-matrix S15 columns, total PYGL, and endpoint availability.

All sample-level output is written only to the temporary Actions artifact. No
clinical association tests or outcome-value exports are performed here.
"""
import csv
import gzip
import hashlib
import io
import json
import os
import re
import zipfile
from collections import defaultdict
from pathlib import Path

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_pdc_pancancer as base

OUT = Path("remote_results")
OUT.mkdir(parents=True, exist_ok=True)
INPUT_ROOT = Path(os.environ.get("S15_INPUT_ROOT", "remote_input/previous"))
CANDIDATE_PDC_STUDIES = {
    "BRCA": ["PDC000121"], "ccRCC": ["PDC000128", "PDC000412"], "COAD": ["PDC000117"],
    "GBM": ["PDC000205", "PDC000448"], "HGSC/OV": ["PDC000119"], "HNSCC": ["PDC000222"],
    "LSCC": ["PDC000232"], "LUAD": ["PDC000149", "PDC000490"], "PDAC": ["PDC000271"],
    "UCEC": ["PDC000126", "PDC000441"], "MB": []
}
TOTAL_PROTEOME = "Proteome_UMich_GENCODE34_v1.zip"
CLINICAL_ARCHIVE = "Clinical_meta_data_v1.zip"
ZENODO_RECORD = "8394329"
PDC_HOSTS = ("https://pdc.cancer.gov/graphql", "https://proteomic.datacommons.cancer.gov/graphql")

MAP_FIELDS = ("pipeline", "cancer", "matrix_column", "patient_id", "pdc_study_id", "pdc_case_id",
              "pdc_case_submitter_id", "pdc_sample_id", "pdc_sample_submitter_id",
              "pdc_aliquot_id", "pdc_aliquot_submitter_id", "sample_type", "pool",
              "PDC_mapping", "PayneLab_mapping", "mapping_agreement", "mapping_method",
              "S15_representation", "S15_value", "S15_numeric_in_source_matrix",
              "S15_observed_status", "total_PYGL_value", "total_PYGL_matched")
TOTAL_FIELDS = ("pipeline", "cancer", "matrix_column", "patient_id", "sample_type", "S15",
                "S15_representation", "S15_observed", "total_PYGL", "matched",
                "mapping_agreement", "technical_replicate_n", "source_matrix_columns", "measurement_source")
ENDPOINT_FIELDS = ("endpoint", "definition", "mapped_S15_patient_n", "nonmissing_endpoint_n",
                   "event_n", "group_counts", "followup_available", "source",
                   "suitable_for_primary", "reason")


def write_tsv(path, fields, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def missing(value):
    return str(value or "").strip().lower() in {"", "na", "n/a", "nan", "null", "none", "not reported", "unknown"}


def scalar_fields_for_type(type_name):
    q = "{ __type(name:%s) { fields { name type { kind name ofType { kind name ofType { kind name } } } } } }" % json.dumps(type_name)
    payload = pdc_graphql(q, "schema_" + type_name)
    fields = ((payload.get("__type") or {}).get("fields") or [])
    out = []
    for item in fields:
        t = item.get("type") or {}
        while t.get("ofType"):
            t = t["ofType"]
        if t.get("kind") in {"SCALAR", "ENUM"} and item.get("name") and not item["name"].startswith("__"):
            out.append(item["name"])
    return out


def pdc_graphql(query, label):
    errors = []
    for host in PDC_HOSTS:
        try:
            response = requests.post(host, json={"query": query}, timeout=150,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                errors.append(str(payload["errors"])[:500])
                continue
            if payload.get("data") is None:
                errors.append("null GraphQL data")
                continue
            return payload["data"]
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:300])
    raise RuntimeError("PDC GraphQL %s failed: %s" % (label, " | ".join(errors)))


def root_type_name(root):
    query = "{ __type(name:\"Query\") { fields { name type { kind name ofType { kind name ofType { kind name } } } } } }"
    data = pdc_graphql(query, "query_schema")
    for item in ((data.get("__type") or {}).get("fields") or []):
        if item.get("name") == root:
            t = item.get("type") or {}
            while t.get("ofType"):
                t = t["ofType"]
            return t.get("name")
    return None


def query_pdc_root(root, study_id):
    type_name = root_type_name(root)
    fields = scalar_fields_for_type(type_name) if type_name else []
    if root == "biospecimenPerStudy" and not fields:
        fields = ["aliquot_id", "sample_id", "case_id", "aliquot_submitter_id", "sample_submitter_id",
                  "case_submitter_id", "aliquot_status", "case_status", "sample_status", "project_name",
                  "sample_type", "disease_type", "primary_site", "pool"]
    if not fields:
        raise RuntimeError("Could not introspect fields for PDC root %s" % root)
    field_text = " ".join(fields)
    queries = []
    for argument in ("pdc_study_id", "study_id"):
        base_args = "%s:%s" % (argument, json.dumps(study_id))
        queries.extend(["{%s(%s acceptDUA:true) {%s}}" % (root, base_args, field_text),
                        "{%s(%s) {%s}}" % (root, base_args, field_text)])
    last = None
    for q in queries:
        try:
            data = pdc_graphql(q, root + "_" + study_id)
            value = data.get(root)
            if isinstance(value, dict):
                value = value.get(root) or value.get("data") or []
            return value or [], fields
        except Exception as exc:
            last = exc
    raise RuntimeError("PDC %s query failed: %s" % (root, last))


def load_previous_s15_sample_ids():
    candidates = [INPUT_ROOT / "PYGL_S15_CONFIRMED.tsv"]
    candidates.extend(INPUT_ROOT.rglob("PYGL_S15_CONFIRMED.tsv") if INPUT_ROOT.exists() else [])
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return set(), "PREVIOUS_ARTIFACT_INPUT_NOT_FOUND"
    ids = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("pipeline") == "UMich" and row.get("cohort") == "BRCA":
                ids.update(x for x in (row.get("sample_ids") or "").split(";") if x)
    return ids, str(path)


def load_sample_measurements():
    path = OUT / "S15_CrossPipeline_SampleLevel.tsv"
    if not path.is_file():
        raise RuntimeError("S15 sample-level census missing: " + str(path))
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("numeric_cell") != "YES" or missing(row.get("value_text")):
                continue
            try:
                value = float(row["value_text"])
            except (ValueError, TypeError):
                continue
            row["s15_value_float"] = value
            rows.append(row)
    return rows


def classify_source_cell_status(row):
    # TMT-Integrator source exports retain sparse quantified cells; MD denotes
    # sample-level median centering, not imputation or matched-total normalization.
    if row.get("pipeline") == "UMich" and "Report_abundance_groupby=" in row.get("archive_member", ""):
        return "OBSERVED_SOURCE_QUANTIFICATION; TMT-Integrator report; missing cells remain missing"
    evidence_path = OUT / "S15_imputation_evidence.tsv"
    if evidence_path.is_file():
        with evidence_path.open(encoding="utf-8-sig", newline="") as handle:
            relevant = [r for r in csv.DictReader(handle, delimiter="\t")
                        if r.get("pipeline") == row.get("pipeline") and r.get("archive") == row.get("archive")]
        text = " ".join(r.get("finding", "") for r in relevant).lower()
        if re.search(r"\b(no|not|without)\s+(imputation|imputed)\b", text):
            return "OBSERVED_SOURCE_QUANTIFICATION; archive documentation explicitly excludes imputation"
        if re.search(r"\b(imputation|imputed|dreamai|knn)\b", text):
            return "IMPUTATION_MENTIONED; cell-level source status unresolved"
    return "SOURCE_VALUE_PRESENT; IMPUTATION_STATUS_UNRESOLVED"


def download_zenodo_asset(name):
    response = requests.get("https://zenodo.org/api/records/" + ZENODO_RECORD, timeout=90)
    response.raise_for_status()
    payload = response.json()
    item = next((f for f in payload.get("files", []) if f.get("key") == name), None)
    if item is None:
        raise RuntimeError("Zenodo record lacks exact file key: " + name)
    url = (item.get("links") or {}).get("self")
    if not url:
        raise RuntimeError("Zenodo file lacks self link: " + name)
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    return r.content, hashlib.sha256(r.content).hexdigest(), url


def paynelab_mapping():
    try:
        raw, sha, url = download_zenodo_asset("brca_mapping.csv")
    except Exception as exc:
        detail = {"mapping_file": "brca_mapping.csv", "mapping_status": "DOWNLOAD_FAILED",
                  "error": type(exc).__name__ + ": " + str(exc)[:300]}
        return {}, {}, {}, detail
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
    mapping = {}
    for row in rows:
        key, patient = row.get("Hash", "").strip(), row.get("Patient_ID", "").strip()
        if key and patient:
            mapping[key.casefold()] = patient
    detail = {"mapping_file": "brca_mapping.csv", "mapping_status": "READ", "sha256": sha,
              "source_url": url, "rows": len(rows)}
    sample_map = {}
    sample_id_map = {}
    try:
        raw2, sha2, url2 = download_zenodo_asset("umich-brca-mapping-prosp-brca-all-samples.txt.gz")
    except Exception:
        try:
            raw2, sha2, url2 = download_zenodo_asset("prosp-brca-all-samples.txt.gz")
        except Exception as exc:
            detail["sample_map_status"] = "NOT_FOUND: " + str(exc)[:250]
            return mapping, sample_map, sample_id_map, detail
    stream = gzip.GzipFile(fileobj=io.BytesIO(raw2), mode="rb")
    text = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace")
    sample_rows = list(csv.DictReader(text, delimiter="\t"))
    detail.update({"sample_map_file": "prosp-brca-all-samples.txt.gz", "sample_map_sha256": sha2,
                   "sample_map_source_url": url2, "sample_map_rows": len(sample_rows), "sample_map_status": "READ"})
    for row in sample_rows:
        participant = (row.get("Participant") or "").strip()
        if participant.startswith("X"):
            participant = participant[1:]
        sample_map.setdefault(participant, []).append(row)
        sample_id = (row.get("id") or "").strip()
        if sample_id:
            sample_id_map[sample_id.casefold()] = row
    return mapping, sample_map, sample_id_map, detail


def all_pdc_study_metadata(study_ids, include_clinical=False):
    result = {"biospecimenPerStudy": [], "studyExperimentalDesign": [], "clinicalMetadata": [],
              "clinicalPerStudy": [], "errors": [], "study_ids": list(study_ids)}
    roots = ["biospecimenPerStudy", "studyExperimentalDesign"]
    if include_clinical:
        roots.extend(["clinicalMetadata", "clinicalPerStudy"])
    for study_id in study_ids:
        for root in roots:
            try:
                rows, _ = query_pdc_root(root, study_id)
                for row in rows if isinstance(rows, list) else [rows]:
                    if isinstance(row, dict):
                        result[root].append({**row, "_source_pdc_study_id": study_id})
            except Exception as exc:
                result["errors"].append(study_id + " " + root + ": " + str(exc)[:300])
    return result


def pdc_match(matrix_column, data):
    # Exact identifier lookup first. A _D1/_D2 suffix is never removed.
    exact_rows = []
    for root in ("biospecimenPerStudy", "studyExperimentalDesign", "clinicalMetadata", "clinicalPerStudy"):
        for row in data.get(root, []):
            for key, value in row.items():
                if value is not None and str(value).strip().casefold() == matrix_column.strip().casefold():
                    exact_rows.append((root, row, key))
                    break
    if exact_rows:
        case_ids = {str(x[1].get("case_id") or x[1].get("case_submitter_id") or "").casefold() for x in exact_rows
                    if not missing(x[1].get("case_id") or x[1].get("case_submitter_id"))}
        if len(case_ids) > 1:
            return {}, "AMBIGUOUS_EXACT_IDENTIFIER_MATCH_MULTIPLE_CASES"
        return merge_pdc_rows([x[1] for x in exact_rows]), "EXACT_IDENTIFIER_LOOKUP"
    if re.search(r"_D[12]$", matrix_column, re.I):
        return {}, "UNMAPPED_SUFFIXED_COLUMN; SUFFIX_PRESERVED"
    # Truncated UUID prefix lookup is reported explicitly and is never treated as exact.
    prefix_candidates = []
    for row in data.get("biospecimenPerStudy", []):
        for key in ("aliquot_id", "sample_id", "case_id"):
            value = str(row.get(key) or "").strip()
            if value and value.casefold().startswith(matrix_column.casefold()):
                prefix_candidates.append(row)
                break
    unique = {str(row.get("aliquot_id") or row.get("sample_id") or row.get("case_id")): row for row in prefix_candidates}
    if len(unique) == 1:
        return merge_pdc_rows(list(unique.values())), "UNIQUE_UUID_PREFIX_ONLY; NOT_EXACT"
    return {}, "NO_EXACT_PDC_IDENTIFIER_MATCH"


def merge_pdc_rows(rows):
    merged = {}
    for row in rows:
        for key, value in row.items():
            if not missing(value) and (key not in merged or missing(merged[key])):
                merged[key] = value
    return merged


def download_total_pygl():
    records, _, _, _, error = base.query_metadata(TOTAL_PROTEOME)
    rec = next((r for r in records if r.get("file_name") == TOTAL_PROTEOME), None)
    if not rec:
        raise RuntimeError("PDC PanCancer metadata missing total proteome archive: " + str(error))
    path, ok, size, sha, http, error = base.download(TOTAL_PROTEOME, rec.get("file_id"))
    if not ok:
        raise RuntimeError("Could not download total proteome archive: HTTP %s %s" % (http, error))
    values, rows_found = {}, []
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir() and "brca" in m.filename.lower()
                   and "protein" in m.filename.lower() and m.filename.lower().endswith((".tsv", ".txt", ".csv", ".tsv.gz", ".csv.gz"))]
        for member in members:
            with archive.open(member) as raw:
                stream = gzip.GzipFile(fileobj=raw, mode="rb") if member.filename.lower().endswith(".gz") else raw
                header, rows, _, note = base.read_delimited(stream, member.filename)
                if not header:
                    continue
                for row in rows:
                    row = ["" if v is None else str(v) for v in row]
                    joined = " ".join(row[:min(len(row), 40)]).upper()
                    if not (re.search(r"\bPYGL\b", joined) or "P06737" in joined or "ENSG00000100504" in joined):
                        continue
                    _, sample_idx = base.split_columns(header, row)
                    score = (3 if "P06737" in joined else 0) + (3 if "ENSG00000100504" in joined else 0) + (2 if re.search(r"\bPYGL\b", joined) else 0)
                    rows_found.append((score, member.filename, row, sample_idx))
                if stream is not raw:
                    stream.close()
    if not rows_found:
        raise RuntimeError("No BRCA total-PYGL protein row found in UMich proteome archive")
    top = max(x[0] for x in rows_found)
    selected = [x for x in rows_found if x[0] == top]
    # A single highest-confidence protein group is required; ambiguous rows remain unmapped.
    if len(selected) != 1:
        return {}, {"status": "AMBIGUOUS_ROWS", "candidate_rows": len(selected), "sha256": sha, "size": size}
    _, member_name, row, idx = selected[0]
    values = {}
    # Re-open only the selected matrix member to pair values with exact column names.
    with zipfile.ZipFile(path) as archive:
        with archive.open(member_name) as raw:
            stream = gzip.GzipFile(fileobj=raw, mode="rb") if member_name.lower().endswith(".gz") else raw
            header, rows, _, _ = base.read_delimited(stream, member_name)
            candidates = []
            for current in rows:
                current = ["" if v is None else str(v) for v in current]
                joined = " ".join(current[:min(len(current), 40)]).upper()
                score = (3 if "P06737" in joined else 0) + (3 if "ENSG00000100504" in joined else 0) + (2 if re.search(r"\bPYGL\b", joined) else 0)
                if score == top:
                    candidates.append(current)
            if candidates:
                row = candidates[0]
            _, sample_idx = base.split_columns(header, row)
            values = {str(header[i]): row[i] for i in sample_idx if i < len(row)}
            if stream is not raw:
                stream.close()
    path.unlink(missing_ok=True)
    return values, {"status": "UNIQUE_ROW", "matrix_member": member_name, "candidate_rows": len(selected),
                   "sha256": sha, "size": size}


def parse_clinical_archive(patient_ids):
    # Clinical values stay in memory; only availability counts and group counts are exported.
    records = defaultdict(list)
    audit = {"status": "NOT_ATTEMPTED", "members_read": 0, "matching_rows": 0, "error": ""}
    meta, _, _, _, error = base.query_metadata(CLINICAL_ARCHIVE)
    rec = next((r for r in meta if r.get("file_name") == CLINICAL_ARCHIVE), None)
    if not rec:
        audit.update({"status": "METADATA_FAILED", "error": str(error)[:400]})
        return records, audit
    path, ok, size, sha, http, error = base.download(CLINICAL_ARCHIVE, rec.get("file_id"))
    if not ok:
        audit.update({"status": "DOWNLOAD_FAILED", "error": "HTTP %s %s" % (http, error)[:400]})
        return records, audit
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                low = member.filename.lower()
                if member.is_dir() or member.file_size > 200_000_000 or not low.endswith((".tsv", ".csv", ".txt", ".tsv.gz", ".csv.gz")):
                    continue
                if "clinical" not in low and "brca" not in low:
                    continue
                try:
                    with archive.open(member) as raw:
                        stream = gzip.GzipFile(fileobj=raw, mode="rb") if low.endswith(".gz") else raw
                        header, rows, _, _ = base.read_delimited(stream, member.filename)
                        if not header:
                            continue
                        audit["members_read"] += 1
                        id_idxs = [i for i, name in enumerate(header) if re.search(r"case|patient|participant|submitter|cptac|participant_id", name, re.I)]
                        for row in rows:
                            values = ["" if v is None else str(v) for v in row]
                            found = {values[i].strip() for i in id_idxs if i < len(values)} & patient_ids
                            for patient in found:
                                records[patient].append({str(header[i]): (values[i] if i < len(values) else "") for i in range(len(header))})
                                audit["matching_rows"] += 1
                        if stream is not raw:
                            stream.close()
                except Exception:
                    continue
        audit.update({"status": "PARSED", "archive_sha256": sha, "size_bytes": size, "http_status": http})
    except Exception as exc:
        audit.update({"status": "PARSE_FAILED", "error": type(exc).__name__ + ": " + str(exc)[:300]})
    finally:
        path.unlink(missing_ok=True)
    return records, audit


def endpoint_values(patient, pdc_case, clinical_records, pdc_records):
    found = []
    found.extend(clinical_records.get(patient, []))
    if pdc_case and pdc_case in clinical_records:
        found.extend(clinical_records[pdc_case])
    for row in pdc_records:
        keys = {str(v).strip() for k, v in row.items() if "case" in k.lower() or "submitter" in k.lower()}
        if patient in keys or (pdc_case and pdc_case in keys):
            found.append(row)
    out = {}
    for row in found:
        for key, value in row.items():
            if not missing(value):
                out.setdefault(norm(key), value)
    return out


def endpoint_inventory(patients, patient_pdc, clinical_records, pdc_clinical):
    definitions = {
        "overall_survival": ("overall survival/time-to-death endpoint", ("overall_survival", "os", "days_to_death", "vital_status")),
        "progression_free_survival": ("progression/recurrence-free time-to-event endpoint", ("progression_free_survival", "pfs", "progression_or_recurrence", "days_to_recurrence")),
        "vital_status": ("vital status", ("vital_status",)),
        "tumor_stage": ("clinical/pathologic stage", ("tumor_stage", "ajcc_pathologic_stage", "ajcc_clinical_stage", "stage")),
        "tumor_grade": ("tumor grade", ("tumor_grade", "grade")),
        "ER_status": ("estrogen receptor status", ("er_status", "estrogen_receptor", "erstatusbyihc")),
        "PR_status": ("progesterone receptor status", ("pr_status", "progesterone_receptor", "prstatusbyihc")),
        "HER2_status": ("HER2 status", ("her2_status", "her2", "her2statusbyihc")),
        "triple_negative_status": ("triple-negative breast cancer classification", ("triple_negative", "tnbc")),
        "PAM50_subtype": ("PAM50 molecular subtype", ("pam50", "molecular_subtype", "subtype")),
    }
    by_patient = {p: endpoint_values(p, patient_pdc.get(p, ""), clinical_records, pdc_clinical) for p in patients}
    rows = []
    for endpoint, (definition, aliases) in definitions.items():
        vals = []
        source_fields = set()
        followup = 0
        events = 0
        for values in by_patient.values():
            normalized_aliases = [norm(alias) for alias in aliases]
            selected = next(((k, v) for k, v in values.items() if any(a in k for a in normalized_aliases)), None)
            if any(not missing(values.get(token)) for token in ("daystodeath", "daystolastfollowup", "followuptime", "overallsurvival")):
                followup += 1
            if endpoint == "overall_survival":
                vital = norm(values.get("vitalstatus", ""))
                is_dead = vital in {"dead", "deceased", "1", "yes", "true"}
                is_alive = vital in {"alive", "living", "0", "no", "false"}
                time_value = (values.get("daystodeath") if is_dead else values.get("daystolastfollowup"))
                time_value = time_value or values.get("overallsurvival") or values.get("followuptime")
                complete = (is_dead or is_alive) and not missing(time_value)
                if complete:
                    vals.append(vital)
                    source_fields.update(k for k in ("vitalstatus", "daystodeath", "daystolastfollowup", "overallsurvival", "followuptime") if k in values)
                if complete and is_dead:
                    events += 1
            elif endpoint == "progression_free_survival":
                event = next((values.get(k) for k in ("progressionfreesurvivalevent", "progressionfreeevent", "progressionorrecurrence", "pfsstatus") if values.get(k)), None)
                time_value = next((values.get(k) for k in ("progressionfreesurvivaltime", "daystoprogression", "daystorecurrence", "progressionfreeinterval", "pfstime") if values.get(k)), None)
                event_norm = norm(event)
                known_event = event_norm in {"1", "yes", "true", "event", "progression", "recurrence", "progressionorrecurrence"}
                known_censored = event_norm in {"0", "no", "false", "censored", "nonevent", "norecurrence"}
                complete = (known_event or known_censored) and not missing(time_value)
                if complete:
                    vals.append("event" if known_event else "censored")
                    source_fields.update(k for k in ("progressionfreesurvivalevent", "progressionfreeevent", "progressionorrecurrence", "pfsstatus", "progressionfreesurvivaltime", "daystoprogression", "daystorecurrence", "progressionfreeinterval", "pfstime") if k in values)
                if complete and known_event:
                    events += 1
            elif selected:
                source_fields.add(selected[0]); vals.append(str(selected[1]))
        counts = defaultdict(int)
        for value in vals:
            counts[value] += 1
        nonmissing_n = len(vals)
        if endpoint in {"overall_survival", "progression_free_survival"}:
            suitable = "YES" if nonmissing_n >= 15 and events >= 10 else "NO"
            reason = "Predefined gate: at least 15 mapped patients with both time and event fields, and at least 10 events."
        else:
            suitable = "YES" if nonmissing_n >= 15 and len(counts) >= 2 and min(counts.values(), default=0) >= 5 else "LIMITED"
            reason = "Predefined gate: >=15 mapped patients, >=2 groups, and >=5 patients in each observed group; otherwise exploratory only."
        rows.append({"endpoint": endpoint, "definition": definition, "mapped_S15_patient_n": len(patients),
                     "nonmissing_endpoint_n": nonmissing_n, "event_n": events if endpoint in {"overall_survival", "progression_free_survival"} else "",
                     "group_counts": "" if endpoint in {"overall_survival", "progression_free_survival"} else ";".join("%s=%s" % (k, v) for k, v in sorted(counts.items())),
                     "followup_available": followup, "source": "Clinical_meta_data_v1.zip + PDC PDC000121 clinicalMetadata/clinicalPerStudy",
                     "suitable_for_primary": suitable,
                     "reason": (reason + (" source_fields=" + ",".join(sorted(source_fields)) if source_fields else " No matching endpoint field found."))})
    availability = {}
    for patient, values in by_patient.items():
        availability[patient] = {}
        for endpoint, (_, aliases) in definitions.items():
            normalized_aliases = [norm(alias) for alias in aliases]
            if endpoint == "overall_survival":
                vital = norm(values.get("vitalstatus", ""))
                dead = vital in {"dead", "deceased", "1", "yes", "true"}
                alive = vital in {"alive", "living", "0", "no", "false"}
                time_value = (values.get("daystodeath") if dead else values.get("daystolastfollowup"))
                time_value = time_value or values.get("overallsurvival") or values.get("followuptime")
                available = (dead or alive) and not missing(time_value)
            elif endpoint == "progression_free_survival":
                event = next((values.get(k) for k in ("progressionfreesurvivalevent", "progressionfreeevent", "progressionorrecurrence", "pfsstatus") if values.get(k)), None)
                time_value = next((values.get(k) for k in ("progressionfreesurvivaltime", "daystoprogression", "daystorecurrence", "progressionfreeinterval", "pfstime") if values.get(k)), None)
                event_norm = norm(event)
                available = (event_norm in {"1", "yes", "true", "event", "progression", "recurrence", "progressionorrecurrence", "0", "no", "false", "censored", "nonevent", "norecurrence"}) and not missing(time_value)
            else:
                available = any(any(alias in key for alias in normalized_aliases) and not missing(value)
                                for key, value in values.items())
            availability[patient][endpoint] = bool(available)
    return rows, by_patient, availability


def main():
    previous_ids, previous_source = load_previous_s15_sample_ids()
    measurements = load_sample_measurements()
    previous_reference_ids = {x for x in previous_ids if re.search(r"(?i)(refint|pool|bridge)", x)}
    previous_biological_ids = previous_ids - previous_reference_ids
    current_umich_brca_rows = [r for r in measurements if r.get("pipeline") == "UMich" and
                               r.get("cancer") == "BRCA" and r.get("representation") == "multi-site"]
    current_umich_brca_ids = {r["matrix_column"] for r in current_umich_brca_rows
                              if str(r.get("is_reference", "")).upper() != "YES"}
    current_reference_ids = {r["matrix_column"] for r in current_umich_brca_rows
                             if str(r.get("is_reference", "")).upper() == "YES"}
    id_reconciliation = {"previous_table_path": previous_source, "previous_nonmissing_columns": len(previous_ids),
        "reference_pool_columns": sorted(previous_reference_ids), "previous_biological_columns": len(previous_biological_ids),
        "current_multi_site_numeric_columns": len(current_umich_brca_rows),
        "current_reference_pool_columns": sorted(current_reference_ids),
        "current_biological_columns": len(current_umich_brca_ids),
        "previous_ids_missing_from_current": sorted(previous_biological_ids - current_umich_brca_ids),
        "current_ids_not_in_previous": sorted(current_umich_brca_ids - previous_biological_ids),
        "exact_id_set_match_after_reference_exclusion": previous_biological_ids == current_umich_brca_ids}
    payne, payne_sample_map, payne_sample_id_map, payne_audit = paynelab_mapping()
    pdc_by_cohort = {cancer: all_pdc_study_metadata(studies, include_clinical=(cancer == "BRCA"))
                     for cancer, studies in CANDIDATE_PDC_STUDIES.items()}
    sample_rows = []
    # One row per pipeline/cohort/sample; prefer the standard multi-site representation.
    preference = {"multi-site": 0, "single-site": 1, "peptide": 2}
    chosen = {}
    for row in measurements:
        key = (row.get("pipeline", ""), row.get("cancer", ""), row.get("matrix_column", ""))
        if key not in chosen or preference.get(row.get("representation", ""), 9) < preference.get(chosen[key].get("representation", ""), 9):
            chosen[key] = row

    pdc_records_by_cohort = {c: d.get("biospecimenPerStudy", []) for c, d in pdc_by_cohort.items()}
    for (pipeline, cancer, matrix_column), row in sorted(chosen.items()):
        study_meta = pdc_by_cohort.get(cancer, {})
        pdc, pdc_method = pdc_match(matrix_column, study_meta) if study_meta else ({}, "PDC_STUDY_NOT_QUERIED")
        payne_patient = payne.get(matrix_column.casefold(), "") if cancer == "BRCA" and pipeline == "UMich" else ""
        payne_row = payne_sample_id_map.get(matrix_column.casefold(), {}) if cancer == "BRCA" and pipeline == "UMich" else {}
        if not pdc and payne_patient:
            case_matches = [r for r in pdc_records_by_cohort.get(cancer, [])
                            if str(r.get("case_submitter_id") or "").casefold() == payne_patient.casefold()]
            if case_matches:
                cases = {str(r.get("case_id") or "") for r in case_matches if not missing(r.get("case_id"))}
                pdc = {"case_submitter_id": payne_patient}
                if len(cases) == 1:
                    pdc["case_id"] = next(iter(cases))
                pdc_method = "EXACT_CASE_SUBMITTER_ID_AFTER_EXACT_PAYNELAB_HASH_MAPPING"
        pdc_case = str(pdc.get("case_submitter_id") or "")
        agreement = "CONCORDANT" if payne_patient and pdc_case and payne_patient.casefold() == pdc_case.casefold() else (
            "PDC_ONLY" if pdc_case else ("PAYNELAB_ONLY" if payne_patient else "UNMAPPED"))
        patient = payne_patient or pdc_case
        sample_type = str(pdc.get("sample_type") or payne_row.get("Type") or "")
        pdc_flag = "EXACT" if pdc_method.startswith("EXACT") else pdc_method
        s15_source = row.get("source_value_status", "")
        sample_rows.append({"pipeline": pipeline, "cancer": cancer, "matrix_column": matrix_column,
            "patient_id": patient, "pdc_study_id": pdc.get("_source_pdc_study_id", ""), "pdc_case_id": pdc.get("case_id", ""), "pdc_case_submitter_id": pdc_case,
            "pdc_sample_id": pdc.get("sample_id", ""), "pdc_sample_submitter_id": pdc.get("sample_submitter_id", ""),
            "pdc_aliquot_id": pdc.get("aliquot_id", ""), "pdc_aliquot_submitter_id": pdc.get("aliquot_submitter_id", ""),
            "sample_type": sample_type, "pool": pdc.get("pool", ""), "PDC_mapping": pdc_flag,
            "PayneLab_mapping": "EXACT_HASH" if payne_patient else ("NOT_FOUND" if cancer == "BRCA" and pipeline == "UMich" else "NOT_APPLICABLE"),
            "mapping_agreement": agreement, "mapping_method": pdc_method + ("; PayneLab exact Hash->Patient_ID" if payne_patient else ""),
            "S15_representation": row.get("representation", ""), "S15_value": row.get("value_text", ""),
            "S15_numeric_in_source_matrix": "YES", "S15_observed_status": classify_source_cell_status(row),
            "total_PYGL_value": "", "total_PYGL_matched": "NO"})

    # Download and parse the total-proteome archive; match only identical matrix column IDs.
    try:
        total_values, total_audit = download_total_pygl()
    except Exception as exc:
        total_values, total_audit = {}, {"status": "FAILED", "error": type(exc).__name__ + ": " + str(exc)[:400]}
    for row in sample_rows:
        value = total_values.get(row["matrix_column"])
        if value is not None and not missing(value):
            try:
                float(value)
                row["total_PYGL_value"] = value
                row["total_PYGL_matched"] = "YES"
            except (TypeError, ValueError):
                pass

    # Availability only: clinical outcome values are parsed in memory and not exported.
    brca_patients = {r["patient_id"] for r in sample_rows if r["cancer"] == "BRCA" and r["pipeline"] == "UMich"
                     and r["patient_id"] and r["mapping_agreement"] == "CONCORDANT"}
    clinical_records, clinical_audit = parse_clinical_archive(brca_patients)
    pdc_clinical = []
    for root in ("clinicalMetadata", "clinicalPerStudy"):
        pdc_clinical.extend(pdc_by_cohort.get("BRCA", {}).get(root, []))
    aliquot_to_case = {}
    for item in pdc_records_by_cohort.get("BRCA", []):
        case = str(item.get("case_submitter_id") or "")
        if not case:
            continue
        for key in ("aliquot_submitter_id", "aliquot_id", "sample_submitter_id", "sample_id"):
            value = str(item.get(key) or "")
            if value:
                aliquot_to_case[value.casefold()] = case
    pdc_clinical = [({**row, "case_submitter_id": row.get("case_submitter_id") or
                      aliquot_to_case.get(str(row.get("aliquot_submitter_id") or row.get("aliquot_id") or
                                               row.get("sample_submitter_id") or row.get("sample_id") or "").casefold(), "")})
                    for row in pdc_clinical]
    patient_pdc = {r["patient_id"]: r["pdc_case_submitter_id"] for r in sample_rows if r["patient_id"] and r["pdc_case_submitter_id"]}
    endpoint_rows, endpoint_by_patient, endpoint_availability = endpoint_inventory(brca_patients, patient_pdc, clinical_records, pdc_clinical)
    for row in sample_rows:
        if row["cancer"] == "BRCA" and row["pipeline"] == "UMich" and row["patient_id"]:
            values = endpoint_availability.get(row["patient_id"], {})
            for endpoint in ("overall_survival", "progression_free_survival", "vital_status", "tumor_stage", "tumor_grade", "ER_status", "PR_status", "HER2_status", "triple_negative_status", "PAM50_subtype"):
                row["endpoint_" + endpoint + "_available"] = "YES" if values.get(endpoint, False) else "NO"

    update_cohort_census(sample_rows)

    write_tsv(OUT / "BRCA_S15_sample_to_patient_map.tsv", MAP_FIELDS + tuple("endpoint_" + e + "_available" for e in
        ("overall_survival", "progression_free_survival", "vital_status", "tumor_stage", "tumor_grade", "ER_status", "PR_status", "HER2_status", "triple_negative_status", "PAM50_subtype")),
        [r for r in sample_rows if r["cancer"] == "BRCA" and r["pipeline"] == "UMich"])
    all_map_fields = list(MAP_FIELDS) + ["endpoint_" + e + "_available" for e in
        ("overall_survival", "progression_free_survival", "vital_status", "tumor_stage", "tumor_grade", "ER_status", "PR_status", "HER2_status", "triple_negative_status", "PAM50_subtype")]
    write_tsv(OUT / "S15_CrossPipeline_Sample_to_Patient.tsv", all_map_fields, sample_rows)
    patient_units = aggregate_confirmed_technical_replicates(sample_rows)
    write_tsv(OUT / "S15_totalPYGL_patient_matched.tsv", TOTAL_FIELDS, patient_units)
    write_tsv(OUT / "BRCA_endpoint_availability.tsv", ENDPOINT_FIELDS, endpoint_rows)
    write_tsv(OUT / "S15_ENDPOINT_INVENTORY.tsv", ENDPOINT_FIELDS, endpoint_rows)

    umich_brca = [r for r in measurements if r.get("pipeline") == "UMich" and r.get("cancer") == "BRCA"]
    concordance = representation_concordance(umich_brca)
    (OUT / "S15_REPRESENTATION_FREEZE.json").write_text(json.dumps(concordance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    exact_mapped = [r for r in sample_rows if r["cancer"] == "BRCA" and r["pipeline"] == "UMich" and r["mapping_agreement"] == "CONCORDANT"]
    exact_units = [r for r in patient_units if r["mapping_agreement"] == "CONCORDANT"]
    matched_total_n = sum(1 for r in exact_units if r["matched"] == "YES")
    tumor_patients = {r["patient_id"] for r in exact_mapped if "tumor" in norm(r["sample_type"]) and r["patient_id"]}
    normal_patients = {r["patient_id"] for r in exact_mapped if ("normal" in norm(r["sample_type"]) or "adjacentnormal" in norm(r["sample_type"])) and r["patient_id"]}
    unknown_type_patients = {r["patient_id"] for r in exact_mapped if not r["sample_type"] and r["patient_id"]}
    technical_replicate_units = sum(1 for r in patient_units if int(r.get("technical_replicate_n", 1)) > 1)
    observed_patients = {r["patient_id"] for r in exact_mapped if "OBSERVED_SOURCE_QUANTIFICATION" in r["S15_observed_status"] and
                         "tumor" in norm(r["sample_type"]) and r["patient_id"]}
    observed_n = len(observed_patients)
    imputed_n = len({r["patient_id"] for r in exact_mapped if r["S15_observed_status"].startswith("IMPUTED") and r["patient_id"]})
    unresolved_n = len({r["patient_id"] for r in exact_mapped if "UNRESOLVED" in r["S15_observed_status"] and r["patient_id"]})
    primary_endpoint = next((r for r in endpoint_rows if r["suitable_for_primary"] == "YES"), None)
    freeze = {
        "freeze_status": "PROVISIONAL_ROUTE_SELECTION; OTHER_PIPELINE_MAPPING_AND_IMPUTATION_REVIEW_REQUIRED",
        "primary_cancer": "BRCA" if exact_mapped else None,
        "primary_pipeline": "UMich" if exact_mapped else None,
        "primary_representation": "multi-site_protNorm=MD_gu=2" if concordance.get("multi_site_selected") else None,
        "patient_n_concordant_PDC_and_PayneLab": len({r["patient_id"] for r in exact_mapped}),
        "primary_tumor_patient_n": len(tumor_patients), "adjacent_normal_patient_n": len(normal_patients),
        "sample_type_unknown_patient_n": len(unknown_type_patients),
        "exact_mapped_matrix_columns": len({r["matrix_column"] for r in exact_mapped}),
        "technical_replicate_sample_units": technical_replicate_units,
        "observed_patient_n": observed_n,
        "imputed_patient_n": imputed_n,
        "numeric_source_patient_n_unresolved": unresolved_n,
        "matched_total_PYGL_n": matched_total_n,
        "primary_endpoint": primary_endpoint.get("endpoint") if primary_endpoint else None,
        "primary_endpoint_status": "FROZEN_BY_PREDEFINED_COMPLETENESS_RULE" if primary_endpoint else "NO_ENDPOINT_PASSES_PREDEFINED_GATE",
        "primary_endpoint_rule": "OS/PFS requires >=15 mapped patients and >=10 events; categorical endpoint requires >=15 mapped patients, >=2 groups and >=5 per group.",
        "protNorm_MD_interpretation": "TMT-Integrator MD is sample-level median centering of normalized log2 ratios. It is not matched total-PYGL normalization; therefore it does not itself implement pS15/total-PYGL adjustment.",
        "primary_route_observed_n": observed_n, "primary_route_imputed_n": imputed_n,
        "reason": "Only exact/concordant patient mappings qualify. Source-matrix numeric cells are not called observed until archive provenance explicitly excludes imputation. No association values or tests were read or computed.",
        "backup_cohort": None,
        "source_files": {"PDC_PanCancer_S15_sample_matrix": "remote_results/S15_CrossPipeline_SampleLevel.tsv",
                         "previous_S15_confirmed_table": previous_source},
        "previous_matrix_column_reconciliation": id_reconciliation,
        "PDC_metadata_errors": pdc_by_cohort.get("BRCA", {}).get("errors", []),
        "PayneLab_resource_audit": payne_audit,
        "total_proteome_audit": total_audit,
        "clinical_archive_audit": clinical_audit,
        "representation_comparison": concordance,
        "endpoint_inventory": endpoint_rows,
        "clinical_analysis": "NOT_RUN; route selection and endpoint inventory only"
    }
    (OUT / "S15_PRIMARY_COHORT_FREEZE.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    model_freeze = {"freeze_status": "NO_ASSOCIATION_ANALYSIS_IN_THIS_RUN", "cohort": freeze["primary_cancer"],
        "pipeline": freeze["primary_pipeline"], "representation": freeze["primary_representation"],
        "patient_inclusion": "exact/concordant PDC and PayneLab patient mapping; primary tumor only; observed S15 only after provenance confirms no imputation",
        "sample_handling": "Average only technical replicate aliquots with identical mapped sample_id and sample_type after PDC design confirms replicate/run identity; keep tumor and adjacent normal separate.",
        "primary_endpoint": freeze["primary_endpoint"], "secondary_endpoints": [r["endpoint"] for r in endpoint_rows if r["endpoint"] != freeze["primary_endpoint"]],
        "covariates": [], "statistical_model": "Not executed. Endpoint/model cannot be finalized until observed-vs-imputed provenance is resolved and route freeze is accepted.",
        "multiple_testing_family": "One predeclared primary endpoint; endpoint inventory is descriptive only.",
        "association_analysis": "PROHIBITED_BEFORE_COMPLETE_ROUTE_FREEZE"}
    (OUT / "S15_CLINICAL_ANALYSIS_FREEZE.json").write_text(json.dumps(model_freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    checksums = []
    for name in ("S15_REPRESENTATION_FREEZE.json", "S15_PRIMARY_COHORT_FREEZE.json", "S15_CLINICAL_ANALYSIS_FREEZE.json"):
        path = OUT / name
        checksums.append("%s  %s" % (hashlib.sha256(path.read_bytes()).hexdigest(), name))
    (OUT / "S15_FREEZE_SHA256.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    (OUT / "S15_ROUTE_FREEZE_STATUS.txt").write_text(
        "PDC_STUDY_METADATA=queried\nPREVIOUS_UMICH_S15_COLUMNS=%d\nPREVIOUS_BIOLOGICAL_COLUMNS=%d\nCURRENT_MULTI_SITE_NUMERIC_COLUMNS=%d\n" % (len(previous_ids), len(previous_biological_ids), len(current_umich_brca_rows)) +
        "PREVIOUS_ID_SOURCE=%s\nEXACT_ID_SET_MATCH=%s\n" % (previous_source, id_reconciliation["exact_id_set_match_after_reference_exclusion"]) +
        "S15_NUMERIC_CELLS=%d\nCONCORDANT_BRCA_PATIENTS=%d\nMATCHED_TOTAL_PYGL=%d\n" % (len(measurements), len({r['patient_id'] for r in exact_mapped}), matched_total_n) +
        "PRIMARY_TUMOR_PATIENTS=%d\nADJACENT_NORMAL_PATIENTS=%d\nTECHNICAL_REPLICATE_UNITS=%d\n" % (len(tumor_patients), len(normal_patients), technical_replicate_units) +
        "OBSERVED_PATIENT_N=%d\nIMPUTED_PATIENT_N=%d\nUNRESOLVED_PATIENT_N=%d\n" % (observed_n, imputed_n, unresolved_n) +
        "PRIMARY_ENDPOINT=%s\nCLINICAL_ASSOCIATION=NOT_RUN\n" % (freeze["primary_endpoint"] or "NONE_PASSED") +
        "CROSS_PIPELINE_CENSUS_STATUS=SEE_S15_CrossPipeline_Cohort_Census.tsv\n", encoding="utf-8")
    print((OUT / "S15_ROUTE_FREEZE_STATUS.txt").read_text(encoding="utf-8"), flush=True)


def representation_concordance(rows):
    grouped = defaultdict(dict)
    identities = defaultdict(set)
    for row in rows:
        grouped[row.get("representation", "")][row.get("matrix_column", "")] = row.get("s15_value_float")
        identities[row.get("representation", "")].add(row.get("row_identifier", ""))
    names = ["multi-site", "single-site", "peptide"]
    ref = grouped.get("multi-site", {})
    result = {"multi_site_selected": bool(ref), "primary_representation": "multi-site_protNorm=MD_gu=2" if ref else None,
              "comparison_scope": "UMich BRCA; same matrix columns; no clinical outcomes used", "representations": {}}
    for name in names:
        values = grouped.get(name, {})
        overlap = sorted(set(ref) & set(values)) if name != "multi-site" else sorted(ref)
        equal = all(ref[k] == values[k] for k in overlap) if name != "multi-site" else True
        xs = [ref[k] for k in overlap if ref.get(k) is not None and values.get(k) is not None]
        ys = [values[k] for k in overlap if ref.get(k) is not None and values.get(k) is not None]
        result["representations"][name] = {"numeric_cells": len(values), "overlap_with_multi_site": len(overlap),
            "numeric_equality_to_multi_site": "EXACT" if equal and len(overlap) else "NOT_EQUAL_OR_NO_OVERLAP",
            "missing_pattern_same": bool(ref) and set(ref) == set(values),
            "row_identifier": ";".join(sorted(x for x in identities.get(name, set()) if x)),
            "pearson_r_vs_multi_site": correlation(xs, ys), "spearman_rho_vs_multi_site": correlation(ranks(xs), ranks(ys)),
            "row_identity": "same canonical S15 biological site; distinct table rows" if values else "NOT_FOUND"}
    return result


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for k in order[i:j]:
            out[k] = rank
        i = j
    return out


def correlation(x, y):
    if len(x) < 2 or len(x) != len(y):
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    den = (sum(v * v for v in dx) * sum(v * v for v in dy)) ** 0.5
    return round(sum(a * b for a, b in zip(dx, dy)) / den, 10) if den else None


def aggregate_confirmed_technical_replicates(sample_rows):
    grouped = defaultdict(list)
    for row in sample_rows:
        if row["cancer"] != "BRCA" or row["pipeline"] != "UMich":
            continue
        sample = row.get("pdc_sample_id")
        sample_type = row.get("sample_type", "")
        if sample and sample_type and row["mapping_agreement"] == "CONCORDANT":
            key = (row["patient_id"], sample_type, sample)
        else:
            key = (row["patient_id"], sample_type, row["matrix_column"])
        grouped[key].append(row)
    output = []
    for (patient, sample_type, biological_sample), rows in sorted(grouped.items()):
        def mean(field):
            values = []
            for item in rows:
                try:
                    values.append(float(item[field]))
                except (ValueError, TypeError):
                    pass
            return sum(values) / len(values) if values else None
        same_sample_confirmed = len(rows) > 1 and all(r.get("pdc_sample_id") == biological_sample for r in rows)
        s15_mean, total_mean = mean("S15_value"), mean("total_PYGL_value")
        output.append({"pipeline": "UMich", "cancer": "BRCA", "matrix_column": biological_sample,
            "patient_id": patient, "sample_type": sample_type, "S15": s15_mean if s15_mean is not None else "",
            "S15_representation": "multi-site", "S15_observed": rows[0].get("S15_observed_status", "UNRESOLVED"),
            "total_PYGL": total_mean if total_mean is not None else "",
            "matched": "YES" if s15_mean is not None and total_mean is not None else "NO",
            "mapping_agreement": "CONCORDANT" if all(r["mapping_agreement"] == "CONCORDANT" for r in rows) else "NOT_CONCORDANT",
            "technical_replicate_n": len(rows) if same_sample_confirmed else 1,
            "source_matrix_columns": ";".join(r["matrix_column"] for r in rows),
            "measurement_source": "UMich PanCancer matrices; exact raw matrix-column match; mean only for repeated PDC sample_id and sample_type"})
    return output


def update_cohort_census(sample_rows):
    path = OUT / "S15_CrossPipeline_Cohort_Census.tsv"
    if not path.is_file():
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    for row in rows:
        cohort = [x for x in sample_rows if x["pipeline"] == row.get("pipeline") and x["cancer"] == row.get("cancer")]
        if not cohort:
            continue
        mapped = [x for x in cohort if x["mapping_agreement"] == "CONCORDANT" or x["PDC_mapping"] == "EXACT"]
        patient_ids = {x["patient_id"] for x in mapped if x["patient_id"]}
        tumor_ids = {x["patient_id"] for x in mapped if "tumor" in norm(x["sample_type"]) and x["patient_id"]}
        normal_ids = {x["patient_id"] for x in mapped if "normal" in norm(x["sample_type"]) and x["patient_id"]}
        unique_samples = {x["matrix_column"] for x in mapped}
        replicate_groups = defaultdict(set)
        for item in mapped:
            if item.get("pdc_sample_id") and item.get("sample_type") and item.get("patient_id"):
                replicate_groups[(item["patient_id"], item["sample_type"], item["pdc_sample_id"])].add(item["matrix_column"])
        technical_replicates = sum(max(0, len(columns) - 1) for columns in replicate_groups.values())
        row["exact_mapped_samples"] = len(unique_samples)
        row["unique_patients"] = len(patient_ids)
        row["primary_tumor_patients"] = len(tumor_ids)
        row["adjacent_normal_patients"] = len(normal_ids)
        row["technical_replicates"] = technical_replicates
        row["mapping_status"] = "EXACT_PDC_AND_PAYNELAB_CONCORDANT" if any(x["mapping_agreement"] == "CONCORDANT" for x in mapped) else ("EXACT_PDC_MAPPING" if mapped else "NO_EXACT_PDC_MAPPING")
        observed = {x["patient_id"] for x in mapped if x["patient_id"] and "OBSERVED_SOURCE_QUANTIFICATION" in x["S15_observed_status"]}
        imputed = {x["patient_id"] for x in mapped if x["patient_id"] and x["S15_observed_status"].startswith("IMPUTED")}
        unresolved = {x["patient_id"] for x in mapped if x["patient_id"] and "UNRESOLVED" in x["S15_observed_status"]}
        row["observed_patient_n"] = len(observed)
        row["imputed_patient_n"] = len(imputed)
        row["unresolved_value_patient_n"] = len(unresolved)
        statuses = sorted({x["S15_observed_status"] for x in mapped})
        row["imputed_or_observed"] = "; ".join(statuses) if statuses else "SOURCE_VALUE_STATUS_UNRESOLVED"
    write_tsv(path, list(rows[0].keys()) if rows else [], rows)


if __name__ == "__main__":
    main()
