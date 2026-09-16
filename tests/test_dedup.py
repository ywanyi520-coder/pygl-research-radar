from pathlib import Path

from pygl_radar.dedup import SeenCache, deduplicate_papers
from pygl_radar.models import Paper


def test_dedup_merges_doi_and_pmid_metadata():
    papers = deduplicate_papers([
        Paper(title="A Study", doi="https://doi.org/10.1234/ABC", abstract="abstract", sources=["crossref"]),
        Paper(title="A study", doi="10.1234/abc", pmid="PMID:42", journal="Nature", sources=["pubmed"]),
    ])
    assert len(papers) == 1
    assert papers[0].pmid == "42"
    assert papers[0].journal == "Nature"
    assert papers[0].sources == ["crossref", "pubmed"]


def test_seen_cache_only_tracks_successfully_pushed_papers(tmp_path: Path):
    cache = SeenCache(tmp_path / "seen.json")
    paper = Paper(title="A", doi="10.1234/a")
    assert not cache.contains(paper)
    cache.mark([paper])
    cache.save()
    assert SeenCache(tmp_path / "seen.json").contains(Paper(title="A", doi="10.1234/a"))
