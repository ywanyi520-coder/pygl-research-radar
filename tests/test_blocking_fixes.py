from datetime import datetime, timezone
from pathlib import Path

import yaml

from pygl_radar.digest import render_html
from pygl_radar.feedback import FeedbackState
from pygl_radar.fulltext import FulltextAcquirer, apply_fulltext
from pygl_radar.models import Paper
from pygl_radar.notifiers.base import NotificationResult
from pygl_radar.pipeline import run_radar
from pygl_radar.publishing import PublicationResult
from pygl_radar.review import _prompt, review_paper
from pygl_radar.sources.common import HTTPResponse
from pygl_radar.sources.pubmed import search_pubmed_journal_lane
from pygl_radar.triage import deterministic_triage


class PubMedJournalHTTP:
    def __init__(self):
        self.params = []

    def get_json(self, url, *, params=None):
        self.params.append(params or {})
        return {"esearchresult": {"idlist": ["42"]}}

    def get_text(self, url, *, params=None):
        return HTTPResponse(200, """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>42</PMID><Article><ArticleTitle>Unfamiliar causal design</ArticleTitle><Abstract><AbstractText>Conditioning and rescue logic.</AbstractText></Abstract><Journal><Title>Nature</Title><JournalIssue><PubDate><Year>2026</Year><Month>Sep</Month><Day>16</Day></PubDate></JournalIssue></Journal></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>""", {"Content-Type": "text/xml"})


def test_journal_first_lane_query_does_not_depend_on_topic_terms():
    config = {
        "profile": {"keywords": ["PYGL"], "mechanisms": ["lysosome"]},
        "sources": {
            "window_hours": 48,
            "pubmed": {"max_results": 80},
            "journal_lane": {"enabled": True, "max_results": 40, "exclude_publication_types": ["Review"]},
            "prioritize_journals": ["Nature"],
        },
    }
    http = PubMedJournalHTTP()
    papers = search_pubmed_journal_lane(config, now=datetime(2026, 9, 16, tzinfo=timezone.utc), http=http)
    assert papers and papers[0].sources == ["pubmed-journal"]
    assert '"Nature"[jour]' in http.params[0]["term"]
    assert "PYGL" not in http.params[0]["term"]
    assert '"Review"[pt]' in http.params[0]["term"]
    assert deterministic_triage(Paper(title="Unfamiliar", sources=["pubmed-journal"]), config["profile"])["retain"]


class CaptureLLM:
    model = "test"

    def __init__(self, response=None):
        self.prompt = ""
        self.response = response or '{"core_finding":"Evidence.","direct_relevance":10,"mechanism_relevance":20,"experimental_similarity":80,"transferability":80,"idea_value":70,"evidence_quality":70}'

    def generate(self, prompt, *, system="", max_tokens=0):
        self.prompt = prompt
        return self.response


def test_deep_review_receives_profile_and_samples_late_fulltext():
    profile = {"mechanisms": ["PYGL Ser15"], "experimental_patterns": ["downstream bypass"]}
    late = "RESULTS_LATE rescue and downstream bypass. " * 300
    paper = Paper(title="Transferable paper", journal="Nature", abstract="Short abstract", evidence_level="FULLTEXT_READ", fulltext_text="INTRODUCTION " + ("front matter. " * 500) + late)
    client = CaptureLLM()
    review_paper(paper, client, profile)
    assert "PYGL Ser15" in client.prompt
    assert "downstream bypass" in client.prompt
    assert "RESULTS_LATE" in client.prompt
    assert paper.review["fulltext_reviewed"] is True
    assert "FULLTEXT_READ" in _prompt(paper, profile)


class LandingHTTP:
    def get_json(self, url, *, params=None):
        return {"oa_locations": [{"url_for_landing_page": "https://publisher.example/article"}]}

    def get_text(self, url, *, params=None):
        return HTTPResponse(200, "<html><head><title>Article</title></head><body><h1>Article title</h1><p>" + ("Abstract and metadata only. " * 30) + "</p></body></html>", {"Content-Type": "text/html"})


def test_unpaywall_landing_page_is_not_fulltext():
    paper = Paper(title="A", doi="10.1234/landing", abstract="abstract")
    result = apply_fulltext(paper, FulltextAcquirer(LandingHTTP(), min_text_chars=100), unpaywall_email="reader@example.org")
    assert result.evidence_level == "ABSTRACT_ONLY"
    assert paper.evidence_level == "ABSTRACT_ONLY"


def test_feedback_ingestion_changes_other_paper_with_same_pattern(tmp_path: Path):
    state = FeedbackState(tmp_path / "feedback.json")
    source = Paper(title="A", doi="10.1234/a", journal="Nature", triage={"matched_patterns": ["rescue"]})
    target = Paper(title="B", doi="10.1234/b", journal="Nature", triage={"matched_patterns": ["rescue"]})
    state.remember_papers([source])
    assert state.ingest_comments([{"id": 1, "body": "feedback 10.1234/a idea", "created_at": "2026-09-16T00:00:00Z"}]) == 1
    assert state.ingest_comments([{"id": 1, "body": "feedback 10.1234/a idea"}]) == 0
    assert state.modifier(target) > 0
    state.save()
    assert FeedbackState(tmp_path / "feedback.json").data["preferences"]["pattern:rescue"]["idea"] == 1


def test_html_report_keeps_external_links_clickable():
    paper = Paper(title="A", doi="10.1234/a", publisher_url="https://publisher.example/a", final_score=70, review={"core_finding": "Finding"})
    rendered = render_html([paper], {"retrieved": 1, "deduplicated": 1, "reviewed": 1})
    assert "<a href='https://doi.org/10.1234/a'>DOI</a>" in rendered
    assert "<a href='https://publisher.example/a'>Publisher</a>" in rendered
    assert "<pre" not in rendered


class CaptureNotifier:
    def __init__(self):
        self.url = ""

    def send(self, message, *, url=""):
        self.url = url
        return NotificationResult(True, "capture")


class FakePublisher:
    def publish(self, markdown, *, report_date):
        return PublicationResult(True, f"https://github.com/example/radar/issues/{report_date}", 7)


def test_pipeline_publishes_before_wechat_and_passes_public_url(tmp_path: Path):
    config = {
        "profile": {"keywords": ["PYGL", "macrophage"], "mechanisms": ["PYGL", "lysosome"], "experimental_patterns": ["rescue"]},
        "triage": {"batch_size": 20, "retain": 15}, "fulltext": {"enabled": False},
        "scoring": {"min_score": 40, "top_n": 5}, "output": {"directory": str(tmp_path / "reports")},
        "state": {"seen_cache": str(tmp_path / "seen.json"), "feedback": str(tmp_path / "feedback.json")},
    }
    notifier = CaptureNotifier()
    result = run_radar(config, fixture_path=Path(__file__).parent.parent / "fixtures/sample_papers.json", dry_run=True, notifier=notifier, publisher=FakePublisher())
    assert result.report_url.endswith("/2026-09-16")
    assert notifier.url == result.report_url
    saved = result.markdown_path.read_text(encoding="utf-8")
    assert "反馈格式" in saved


def test_workflow_has_pr_only_ci_and_feedback_cache():
    workflow = Path(__file__).parent.parent / ".github/workflows/daily-radar.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "pull_request:" in text
    assert "if: github.event_name == 'pull_request'" in text
    assert "state/feedback.json" in text
    assert "issues: write" in text
    parsed = yaml.safe_load(text)
    assert "jobs" in parsed and "ci" in parsed["jobs"]
