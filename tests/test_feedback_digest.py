from pathlib import Path

from pygl_radar.digest import render_markdown, render_wechat_message
from pygl_radar.feedback import FeedbackState
from pygl_radar.models import Paper
from pygl_radar.notifiers import MockNotifier


def test_feedback_is_soft_and_labels_are_persisted(tmp_path: Path):
    state = FeedbackState(tmp_path / "feedback.json")
    paper = Paper(title="A", doi="10.1234/a")
    for _ in range(10):
        state.record("10.1234/a", "idea")
    assert state.modifier(paper) == 6.0
    state.save()
    assert FeedbackState(tmp_path / "feedback.json").data["version"] == 1


def test_digest_and_mock_wechat_are_renderable():
    paper = Paper(title="中文测试论文", journal="Nature", publication_date="2026-09-16", evidence_level="ABSTRACT_ONLY", final_score=70, scores={"experimental_similarity": 90}, review={"core_finding": "摘要发现。", "why_recommended": "实验模式可迁移。", "mechanism_mapping": "与吞噬后处理相关。", "transferable_strategy": "阻断后做旁路验证。", "new_hypothesis": "测试候选链。"})
    markdown = render_markdown([paper], {"retrieved": 2, "deduplicated": 1, "reviewed": 1})
    assert "中文测试论文" in markdown
    assert "ABSTRACT_ONLY" in markdown
    notifier = MockNotifier()
    result = notifier.send(render_wechat_message([paper], {"retrieved": 2}))
    assert result.ok and notifier.sent_messages
