"""Telegram notifications (stdlib only). Falls back to logging when not configured."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self._token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self._chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str) -> bool:
        """Returns True if delivered to Telegram. Never raises."""
        if not self.configured:
            log.info("notify: %s", text)
            return False
        try:
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": self._chat_id, "text": text}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            return True
        except Exception as e:  # message deliberately omits the URL, which contains the token
            log.warning("telegram notification failed: %s", type(e).__name__)
            return False
