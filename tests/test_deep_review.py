import json

from pygl_radar.digest import render_markdown
from pygl_radar.models import Paper
from pygl_radar.review import deep_dive_paper


class _LLM:
    model = "test-model"

    def __init__(self, payload):
        self.payload = payload

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


def test_abstract_only_deep_dive_cannot_invent_figures_or_dose():
    paper = Paper(
        title="Abstract-only paper",
        abstract="A pathway changed macrophage clearance.",
        evidence_level="ABSTRACT_ONLY",
        review={"core_finding": "Clearance changed."},
    )
    payload = {
        "background": "Background.",
        "study_design": "Figure 2 used 10 mg treatment.",
        "figure_walkthrough": [{"figure": "Figure 2", "key_result": "Strong effect"}],
        "actionable_ideas": ["Repeat Figure 2 with 10 mg treatment."],
        "supervisor_brief": "Useful paper.",
    }

    deep = deep_dive_paper(paper, _LLM(payload), {"mechanisms": ["PYGL"]})

    assert deep["figure_walkthrough"] == []
    assert deep["figure_evidence_mode"] == "ABSTRACT_ONLY"
    assert "NOT_EVALUABLE" in deep["figure_limitations"]
    assert "Figure 2" not in deep["study_design"]
    assert "10 mg" not in deep["study_design"]
    assert deep["claim_warnings"]


def test_fulltext_deep_dive_records_text_only_figure_provenance():
    paper = Paper(
        title="Full-text paper",
        abstract="",
        evidence_level="FULLTEXT_READ",
        fulltext_text="Results Figure 1. Blocking receptor X reduced uptake. Figure 2. Add-back restored uptake. Discussion.",
        review={"core_finding": "A rescue was observed."},
    )
    payload = {
        "background": "Clearance requires a signaling program.",
        "knowledge_gap": "The causal order was unclear.",
        "scientific_question": "Does receptor X control uptake?",
        "central_hypothesis": "Receptor X is upstream.",
        "study_design": "Block receptor X and then perform add-back.",
        "innovations": ["Uses a rescue to order the pathway."],
        "figure_walkthrough": [
            {"figure": "Figure 1", "question": "Is X required?", "approach": "Block X", "key_result": "Uptake fell", "logic_role": "Necessity", "caveat": "No genetic test", "evidence_basis": "RESULTS_TEXT"},
            {"figure": "Figure 2", "question": "Can the phenotype be rescued?", "approach": "Add-back", "key_result": "Uptake recovered", "logic_role": "Rescue", "caveat": "Partial rescue", "evidence_basis": "RESULTS_TEXT"},
        ],
        "key_controls_and_rescues": ["Blockade followed by add-back."],
        "causal_chain": "X → uptake.",
        "strengths": ["Rescue design."],
        "limitations": ["No in vivo test."],
        "topic_mapping": "Maps to upstream blockade and downstream rescue in PYGL work.",
        "actionable_ideas": [{"idea": "Use downstream bypass", "why": "Orders the pathway", "priority": "HIGH", "minimum_test": "Block upstream then add downstream agonist"}],
        "do_not_overclaim": ["Does not prove PYGL regulation."],
        "supervisor_brief": "这篇文章最值得学的是阻断后再救援。",
    }

    deep = deep_dive_paper(paper, _LLM(payload), {"mechanisms": ["PYGL"]})

    assert len(deep["figure_walkthrough"]) == 2
    assert deep["figure_evidence_mode"] == "TEXT_OR_LEGEND_ONLY"
    assert "视觉" in deep["figure_limitations"] or "图片" in deep["figure_limitations"]
    assert deep["model"] == "test-model"


def test_markdown_contains_journal_club_sections():
    paper = Paper(title="Transferable mechanism", evidence_level="FULLTEXT_READ")
    paper.final_score = 88.0
    paper.scores = {"experimental_similarity": 90.0}
    paper.review = {
        "core_finding": "Core result.",
        "why_recommended": "Transferable logic.",
        "mechanism_mapping": "signal → phenotype",
        "transferable_strategy": "blockade → rescue",
        "new_hypothesis": "test bypass",
        "deep_dive": {
            "background": "Background",
            "knowledge_gap": "Gap",
            "scientific_question": "Question",
            "central_hypothesis": "Hypothesis",
            "study_design": "Design",
            "innovations": ["Innovation"],
            "figure_walkthrough": [],
            "figure_limitations": "TEXT_ONLY",
            "key_controls_and_rescues": ["Rescue"],
            "causal_chain": "A → B",
            "strengths": ["Strength"],
            "limitations": ["Limitation"],
            "topic_mapping": "PYGL mapping",
            "actionable_ideas": ["Actionable idea"],
            "do_not_overclaim": ["Do not overclaim"],
            "supervisor_brief": "Brief",
            "figure_evidence_mode": "TEXT_OR_LEGEND_ONLY",
        },
    }

    text = render_markdown([paper], {"retrieved": 10, "deduplicated": 8, "reviewed": 5, "deep_reviewed": 1})

    for heading in (
        "背景与科学问题",
        "整体实验思路",
        "创新点",
        "逐图逻辑",
        "关键 controls",
        "对当前 PYGL 课题的具体映射",
        "可以直接借鉴的实验设计",
        "这篇文章不能替当前课题证明什么",
        "给老师讲的一段话",
    ):
        assert heading in text
