from pygl_radar.digest import render_markdown
from pygl_radar.models import Paper
from pygl_radar.review import review_paper


class FigureClaimClient:
    model = "test-llm"

    def generate(self, prompt, *, system="", max_tokens=0):
        return '{"core_finding":"Figure 2 shows a rescue at dose 10 mg with n=8. The abstract reports improved processing.","why_recommended":"Figure 3 proves transferability.","mechanism_mapping":"The abstract links soluble signals to lysosome function.","transferable_strategy":"Use the exact sample size from Figure 4.","new_hypothesis":"Test a downstream bypass.","direct_relevance":50,"mechanism_relevance":50,"experimental_similarity":80,"transferability":80,"idea_value":70,"evidence_quality":30}'


def test_abstract_only_cannot_emit_figure_dose_or_sample_claims():
    paper = Paper(title="A paper", abstract="The abstract reports improved processing.", evidence_level="ABSTRACT_ONLY")
    review_paper(paper, FigureClaimClient())
    rendered = render_markdown([paper], {"retrieved": 1, "deduplicated": 1, "reviewed": 1})
    assert "Figure 2" not in rendered
    assert "Figure 3" not in rendered
    assert "Figure 4" not in rendered
    assert "n=8" not in rendered
    assert "10 mg" not in rendered
    assert paper.review["evidence_level"] == "ABSTRACT_ONLY"
    assert paper.review["claim_warnings"]
