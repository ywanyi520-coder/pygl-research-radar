from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class NotificationResult:
    ok: bool
    provider: str
    message_id: str | None = None
    error: str | None = None


class Notifier(Protocol):
    def send(self, message: str) -> NotificationResult: ...


@dataclass
class MockNotifier:
    sent_messages: list[str] = field(default_factory=list)
    provider: str = "mock"

    def send(self, message: str) -> NotificationResult:
        self.sent_messages.append(message)
        return NotificationResult(ok=True, provider=self.provider, message_id="dry-run")
