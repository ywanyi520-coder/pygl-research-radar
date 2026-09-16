from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    model: str

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str: ...


@dataclass
class OpenAICompatibleClient:
    """OpenAI-compatible chat-completions client implemented with stdlib HTTP."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 90.0

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}).encode()
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "pygl-research-radar/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            logger.warning("LLM request failed: %s", exc)
            raise RuntimeError("LLM request failed") from exc
        try:
            return str(payload["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM response did not contain chat content") from exc


class UnavailableLLM:
    model = "deterministic-fallback"

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        raise RuntimeError("LLM credentials are not configured")


def client_from_env(model_env: str, *, timeout: float = 90.0) -> LLMClient:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get(model_env, "").strip() or "gpt-4o-mini"
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    if not api_key:
        return UnavailableLLM()
    return OpenAICompatibleClient(api_key=api_key, model=model, base_url=base_url, timeout=timeout)


def parse_json_response(text: str) -> Any:
    """Parse strict JSON while tolerating a single Markdown code fence."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if len(lines) >= 3 else raw.strip("`")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = min((index for index in (raw.find("{"), raw.find("[")) if index >= 0), default=-1)
        if start >= 0:
            end = max(raw.rfind("}"), raw.rfind("]"))
            if end > start:
                return json.loads(raw[start : end + 1])
        raise
