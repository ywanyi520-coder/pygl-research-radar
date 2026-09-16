from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .base import NotificationResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeChatNotifierConfig:
    app_id: str
    app_secret: str
    open_id: str
    template_id: str
    base_url: str = "https://api.weixin.qq.com"

    @classmethod
    def from_env(cls) -> "WeChatNotifierConfig":
        names = ("WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_OPEN_ID", "WECHAT_TEMPLATE_ID")
        values = {name: os.environ.get(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError("Missing GitHub Actions secrets: " + ", ".join(missing))
        return cls(values["WECHAT_APP_ID"], values["WECHAT_APP_SECRET"], values["WECHAT_OPEN_ID"], values["WECHAT_TEMPLATE_ID"], os.environ.get("WECHAT_API_BASE_URL", "https://api.weixin.qq.com").strip())


class WeChatNotifier:
    """WeChat Official Account test-account/template-message notifier."""

    def __init__(self, config: WeChatNotifierConfig):
        self.config = config

    def _request_json(self, url: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET", headers={"Content-Type": "application/json", "User-Agent": "pygl-research-radar/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def send(self, message: str, *, url: str = "") -> NotificationResult:
        try:
            token_response = self._request_json(
                f"{self.config.base_url.rstrip('/')}/cgi-bin/token",
                params={"grant_type": "client_credential", "appid": self.config.app_id, "secret": self.config.app_secret},
            )
            token = str(token_response.get("access_token") or "")
            if not token:
                return NotificationResult(False, "wechat", error=f"token request failed code={token_response.get('errcode', 'unknown')}")
            payload = {
                "touser": self.config.open_id,
                "template_id": self.config.template_id,
                "data": {"first": {"value": message[:1800], "color": "#17365D"}},
            }
            if url:
                payload["url"] = url
            response = self._request_json(
                f"{self.config.base_url.rstrip('/')}/cgi-bin/message/template/send",
                params={"access_token": token}, payload=payload,
            )
            if int(response.get("errcode", -1)) != 0:
                return NotificationResult(False, "wechat", error=f"send failed code={response.get('errcode')}")
            return NotificationResult(True, "wechat", message_id=str(response.get("msgid") or ""))
        except Exception as exc:
            # Do not log URLs or payloads because they can contain credentials/tokens.
            logger.warning("WeChat push failed: %s", type(exc).__name__)
            return NotificationResult(False, "wechat", error="transport failure")
