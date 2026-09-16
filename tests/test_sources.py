from datetime import datetime, timezone

from pygl_radar.sources.crossref import search_crossref
from pygl_radar.sources.pubmed import search_pubmed
from pygl_radar.sources.common import HTTPResponse


class SourceHTTP:
    def get_json(self, url, *, params=None):
        if "crossref" in url:
            return {"message": {"items": [{"DOI": "10.1234/x", "title": ["Crossref paper"], "abstract": "<jats:p>Abstract</jats:p>", "container-title": ["Nature"], "published": {"date-parts": [[2026, 9, 16]]}, "URL": "https://doi.org/10.1234/x", "author": [{"given": "A", "family": "B"}], "link": []}]}}
        return {"esearchresult": {"idlist": ["42"]}}

    def get_text(self, url, *, params=None):
        return HTTPResponse(200, """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>42</PMID><Article><ArticleTitle>PubMed paper</ArticleTitle><Abstract><AbstractText>Abstract text</AbstractText></Abstract><Journal><Title>Immunity</Title><JournalIssue><PubDate><Year>2026</Year><Month>Sep</Month><Day>16</Day></PubDate></JournalIssue></Journal></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType='doi'>10.1234/p</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>""", {"Content-Type": "text/xml"})


def test_pubmed_and_crossref_adapters_return_canonical_papers():
    config = {"profile": {"keywords": ["macrophage"], "mechanisms": ["PYGL"]}, "sources": {"window_hours": 48, "pubmed": {"max_results": 80}, "crossref": {"max_results": 80}}}
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    pubmed = search_pubmed(config, now=now, http=SourceHTTP())
    crossref = search_crossref(config, now=now, http=SourceHTTP())
    assert pubmed[0].pmid == "42" and pubmed[0].doi == "10.1234/p"
    assert crossref[0].abstract == "Abstract" and crossref[0].journal == "Nature"
