from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pygl_radar.codex_mode import (
    CodexModeError,
    build_codex_instructions,
    hydrate_codex_workspace,
    prepare_codex_workspace,
    validate_publishable_codex_report,
    validate_codex_workspace,
)
from pygl_radar.codex_publishing import render_codex_wechat_message
from pygl_radar.config import DEFAULT_CONFIG
from pygl_radar.fulltext import FulltextResult
from pygl_radar.models import Paper


FIXTURE = Path(__file__).parents[1] / "fixtures" / "sample_papers.json"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path) -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["output"]["directory"] = str(tmp_path / "reports")
    config["state"]["seen_cache"] = str(tmp_path / "seen.json")
    config["state"]["feedback"] = str(tmp_path / "feedback.json")
    return config


def _prepare(tmp_path: Path, *, name: str = "work") -> tuple[dict, dict, Path]:
    config = _config(tmp_path)
    result = prepare_codex_workspace(config, now=NOW, fixture_path=FIXTURE, workspace_root=tmp_path / name)
    candidates = json.loads(result["candidates"].read_text(encoding="utf-8"))
    manifest = json.loads(result["manifest"].read_text(encoding="utf-8"))
    return config, manifest, result["workspace"]


def _write_shortlist(workspace: Path, paper_ids: list[str]) -> Path:
    path = workspace / "shortlist.json"
    path.write_text(json.dumps({"date": "2026-09-16", "papers": [{"paper_id": paper_id, "rationale": "experimental logic"} for paper_id in paper_ids]}), encoding="utf-8")
    return path


def _hydrate_fixture(workspace: Path, paper_ids: list[str], *, evidence_level: str = "ABSTRACT_ONLY") -> None:
    shortlist = _write_shortlist(workspace, paper_ids)

    class FakeAcquirer:
        def acquire(self, paper: Paper, *, unpaywall_email: str = "") -> FulltextResult:
            if evidence_level == "FULLTEXT_READ":
                return FulltextResult("FULLTEXT_READ", "Parsed OA results evidence.", "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/", "pmc-xml", "fixture")
            return FulltextResult("ABSTRACT_ONLY", reason="fixture has no hydrated OA text")

    hydrate_codex_workspace(workspace, shortlist, acquirer=FakeAcquirer())


def _review(paper_id: str, title: str, *, evidence_level: str = "ABSTRACT_ONLY", mode: str | None = None) -> dict:
    abstract = evidence_level == "ABSTRACT_ONLY"
    return {
        "paper_id": paper_id,
        "title": title,
        "evidence_level": evidence_level,
        "figure_evidence_mode": mode or ("ABSTRACT_ONLY" if abstract else "RESULTS_TEXT_ONLY"),
        "scores": {
            "direct_relevance": 35,
            "mechanism_relevance": 55,
            "experimental_similarity": 88,
            "transferability": 84,
            "idea_value": 82,
            "evidence_quality": 40 if abstract else 80,
        },
        "final_score": 69.7,
        "review": {
            "background": "Macrophage clearance biology provides the context.",
            "knowledge_gap": "The causal link remains open.",
            "scientific_question": "Can a soluble signal change processing?",
            "central_hypothesis": "The experimental pattern suggests a testable hypothesis.",
            "study_design": "Use blockade and downstream bypass as a testable design.",
            "innovations": ["A transferable perturbation-to-rescue logic."],
            "figure_walkthrough": [],
            "figure_limitations": "NOT_EVALUABLE: no visual figure evidence.",
            "key_controls_and_rescues": ["Include vehicle, blockade, and rescue controls."],
            "causal_chain": "Signal → PYGL Ser15 → glycogenolysis → post-engulfment processing.",
            "strengths": ["The intervention logic is explicit."],
            "limitations": ["The outside paper does not prove the user's macrophage system."],
            "topic_mapping": "Map cautiously to PYGL Ser15, glycogenolysis, and post-engulfment processing as a hypothesis.",
            "actionable_ideas": [{"idea": "Test a downstream bypass.", "priority": "HIGH", "minimum_test": "Measure processing after blockade and rescue."}],
            "do_not_overclaim": ["Do not claim ATP/P2Y2 or lysosome causality in the user's system."],
            "supervisor_brief": "This is a useful experimental template, not proof of our mechanism.",
        },
    }


def _write_review(workspace: Path, papers: list[dict]) -> Path:
    path = workspace / "codex_reviewed.json"
    path.write_text(json.dumps({"version": 1, "date": "2026-09-16", "papers": papers}, ensure_ascii=False), encoding="utf-8")
    return path


def test_codex_prepare_does_not_create_an_llm_client(monkeypatch, tmp_path: Path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("codex-prepare must not create an LLM client")

    monkeypatch.setattr("pygl_radar.pipeline.client_from_env", fail_if_called)
    _, manifest, workspace = _prepare(tmp_path)
    assert manifest["candidate_count"] == 2
    assert (workspace / "codex_instructions.md").exists()


def test_codex_prepare_falls_back_from_config_yaml_to_example(tmp_path: Path):
    example = tmp_path / "config.example.yaml"
    example.write_text("profile:\n  keywords: [macrophage]\n", encoding="utf-8")
    result = prepare_codex_workspace(tmp_path / "config.yaml", now=NOW, fixture_path=FIXTURE, workspace_root=tmp_path / "work")
    assert result["candidate_count"] == 2


def test_candidate_manifest_is_deterministic_and_contains_provenance(tmp_path: Path):
    _, manifest_one, workspace_one = _prepare(tmp_path / "one")
    _, manifest_two, workspace_two = _prepare(tmp_path / "two")
    assert manifest_one == manifest_two
    assert (workspace_one / "candidates.json").read_bytes() == (workspace_two / "candidates.json").read_bytes()
    candidates = json.loads((workspace_one / "candidates.json").read_text(encoding="utf-8"))["papers"]
    assert {item["discovery_lane"] for item in candidates} == {"topic"}
    assert all(item["paper_id"] and "abstract" in item and "source_provenance" in item for item in candidates)


def test_workspace_is_gitignored():
    assert "work/" in Path(__file__).parents[1].joinpath(".gitignore").read_text(encoding="utf-8").splitlines()


def test_codex_instructions_fail_closed_on_branch_cleanliness_and_commit_scope():
    instructions = build_codex_instructions("2026-09-16", {"mechanisms": ["PYGL"]})
    for required in (
        "git switch main",
        "git pull --ff-only origin main",
        "git branch --show-current",
        "git status --porcelain --untracked-files=no",
        "Never stash, reset, force checkout",
        "Only then stage exactly those two paths",
        "git diff --cached --name-only",
    ):
        assert required in instructions


def test_shortlist_unknown_id_is_rejected(tmp_path: Path):
    _, _, workspace = _prepare(tmp_path)
    with pytest.raises(CodexModeError, match="unknown paper_id"):
        hydrate_codex_workspace(workspace, _write_shortlist(workspace, ["doi:10.9999/hallucinated"]), acquirer=object())


def test_oa_hydration_preserves_evidence_provenance(tmp_path: Path):
    _, manifest, workspace = _prepare(tmp_path)
    candidate_ids = manifest["candidate_ids"][:1]
    _hydrate_fixture(workspace, candidate_ids, evidence_level="FULLTEXT_READ")
    updated = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    evidence_path = workspace / updated["hydration"]["evidence_files"][candidate_ids[0]]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["paper_id"] == candidate_ids[0]
    assert evidence["evidence_level"] == "FULLTEXT_READ"
    assert evidence["retrieval_mode"] == "pmc-xml"
    assert evidence["source_url"].startswith("https://")
    assert "Parsed OA results evidence" in evidence["parsed_text"]


def test_abstract_only_figure_claim_fails_validation(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    review = _review(paper_id, "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation")
    review["review"]["figure_walkthrough"] = [{"figure": "Figure 2", "question": "q", "approach": "a", "key_result": "r", "logic_role": "l", "caveat": "c", "evidence_basis": "ABSTRACT"}]
    _write_review(workspace, [review])
    with pytest.raises(CodexModeError, match="ABSTRACT_ONLY"):
        validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")


def test_out_of_range_score_fails_validation(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    review = _review(paper_id, "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation")
    review["scores"]["idea_value"] = 101
    _write_review(workspace, [review])
    with pytest.raises(CodexModeError, match="between 0 and 100"):
        validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")


def test_hallucinated_review_id_fails_validation(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    review = _review("doi:10.9999/hallucinated", "Hallucinated paper")
    _write_review(workspace, [review])
    with pytest.raises(CodexModeError, match="unknown paper_id"):
        validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")


def test_valid_codex_output_is_sanitized_and_provenanced(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    title = "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation"
    review = _review(paper_id, title)
    review["fulltext_text"] = "SECRET_RAW_FULLTEXT_MUST_NOT_BE_PUBLISHED"
    _write_review(workspace, [review])
    result = validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")
    output = result["json"].read_text(encoding="utf-8")
    assert result["papers"] == 1
    assert "SECRET_RAW_FULLTEXT_MUST_NOT_BE_PUBLISHED" not in output
    data = json.loads(output)
    assert data["review_provider"] == "codex-automation"
    assert data["review_mode"] == "codex-native"
    assert data["papers"][0]["evidence_level"] == "ABSTRACT_ONLY"
    assert {"title", "journal", "publication_date", "doi", "pmid", "evidence_level", "final_score", "background", "knowledge_gap", "scientific_question", "central_hypothesis", "study_design", "innovations", "figure_walkthrough", "key_controls_and_rescues", "causal_chain", "strengths", "limitations", "topic_mapping", "actionable_ideas", "do_not_overclaim", "supervisor_brief"}.issubset(data["papers"][0])
    assert "review" not in data["papers"][0]
    assert result["markdown"].exists()


def test_publishable_codex_report_revalidates_the_sanitized_contract(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    _write_review(workspace, [_review(paper_id, "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation")])
    result = validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")
    report = validate_publishable_codex_report(result["json"])
    assert report["stats"]["recommended"] == 1
    assert "完整科研拆解" in render_codex_wechat_message(report, report_url="https://example.test/latest/")


def test_raw_evidence_cannot_enter_publishable_codex_output(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    _write_review(workspace, [_review(paper_id, "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation")])
    result = validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")
    report = json.loads(result["json"].read_text(encoding="utf-8"))
    report["papers"][0]["parsed_text"] = "raw full text must never publish"
    result["json"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(CodexModeError, match="non-sanitized|raw evidence"):
        validate_publishable_codex_report(result["json"])


def test_invalid_codex_output_fails_the_publish_boundary(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id])
    _write_review(workspace, [_review(paper_id, "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation")])
    result = validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")
    report = json.loads(result["json"].read_text(encoding="utf-8"))
    report["papers"][0]["final_score"] = 101
    result["json"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(CodexModeError, match="between 0 and 100"):
        validate_publishable_codex_report(result["json"])


def test_offline_prepare_hydrate_validate_smoke_with_fulltext_mode(tmp_path: Path):
    config, manifest, workspace = _prepare(tmp_path)
    paper_id = manifest["candidate_ids"][0]
    _hydrate_fixture(workspace, [paper_id], evidence_level="FULLTEXT_READ")
    title = "Conditioned medium fractionation identifies a soluble ligand that restores post-engulfment lysosome maturation"
    review = _review(paper_id, title, evidence_level="FULLTEXT_READ", mode="RESULTS_TEXT_ONLY")
    review["review"]["figure_limitations"] = "Results text was read; the image pixels were not inspected."
    _write_review(workspace, [review])
    result = validate_codex_workspace(workspace, profile=config["profile"], output_root=tmp_path / "codex-output")
    assert result["valid"] is True
    output = json.loads(result["json"].read_text(encoding="utf-8"))
    assert output["stats"]["evidence"]["FULLTEXT_READ"] == 1
