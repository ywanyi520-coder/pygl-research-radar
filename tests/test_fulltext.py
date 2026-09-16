from pygl_radar.fulltext import FulltextAcquirer, apply_fulltext
from pygl_radar.models import Paper
from pygl_radar.sources.common import HTTPResponse


class FakeHTTP:
    def __init__(self, body: str):
        self.body = body

    def get_json(self, url, *, params=None):
        return {"resultList": {"result": [{"pmcid": "PMC123"}]}}

    def get_text(self, url, *, params=None):
        return HTTPResponse(200, self.body, {"Content-Type": "application/xml"})


def test_europe_pmc_fulltext_is_marked_only_after_text_is_parsed():
    body = "<article><body>" + ("Detailed lawful full text about phagosome maturation. " * 12) + "</body></article>"
    paper = Paper(title="A", pmid="123", abstract="short abstract")
    result = apply_fulltext(paper, FulltextAcquirer(FakeHTTP(body), min_text_chars=100))
    assert result.evidence_level == "FULLTEXT_READ"
    assert paper.evidence_level == "FULLTEXT_READ"
    assert paper.fulltext_source_url.endswith("PMC123/fullTextXML")
    assert paper.fulltext_text


def test_blocked_page_falls_back_to_abstract_only():
    paper = Paper(title="A", pmid="123", abstract="abstract")
    result = apply_fulltext( paper, FulltextAcquirer(FakeHTTP("<html>Just a moment... challenge-platform</html>"), min_text_chars=20))
    assert result.evidence_level == "ABSTRACT_ONLY"
    assert paper.evidence_level == "ABSTRACT_ONLY"
    assert not paper.fulltext_text
