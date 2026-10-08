from __future__ import annotations

import json

from pygl_radar.llm import OpenAICompatibleClient


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_deepseek_disables_thinking_and_requests_json(monkeypatch):
    calls: list[dict] = []

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _Response({"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
    )

    assert client.generate("Return JSON", max_tokens=4000) == '{"ok": true}'
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["model"] == "deepseek-flash"


def test_deepseek_empty_json_content_retries_without_response_format(monkeypatch):
    calls: list[dict] = []
    responses = iter([
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": '{"items": []}'}}]},
    ])

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _Response(next(responses))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
    )

    assert client.generate("Return JSON") == '{"items": []}'
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert calls[1]["thinking"] == {"type": "disabled"}
