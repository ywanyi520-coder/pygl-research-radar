from pygl_radar.models import Paper
from pygl_radar.scoring import aggregate_scores, score_paper


def test_experimental_similarity_can_outweigh_keyword_overlap():
    keyword_heavy = aggregate_scores({
        "direct_relevance": 100, "mechanism_relevance": 90,
        "experimental_similarity": 5, "transferability": 5, "idea_value": 10, "evidence_quality": 60,
    })
    transferable = aggregate_scores({
        "direct_relevance": 20, "mechanism_relevance": 35,
        "experimental_similarity": 100, "transferability": 100, "idea_value": 95, "evidence_quality": 60,
    })
    assert transferable.final_score > keyword_heavy.final_score


def test_score_is_machine_readable_and_bounded():
    paper = Paper(title="x", triage={"direct_relevance": 200}, review={"idea_value": -1})
    breakdown = score_paper(paper)
    assert set(paper.scores) == {
        "direct_relevance", "mechanism_relevance", "experimental_similarity",
        "transferability", "idea_value", "evidence_quality",
    }
    assert 0 <= breakdown.final_score <= 100
