"""Telegram commands for the live bot: /status, /pause, /resume, /kill.

Commands are accepted only from the configured chat id. Polling uses the Bot API's getUpdates
with an offset, so each command is handled once. Network errors are logged and ignored; trading
never depends on Telegram being reachable.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

COMMANDS = {"/status", "/pause", "/resume", "/kill"}


class TelegramCommands:
    def __init__(self, token: str | None = None, chat_id: str | None = None, fetch=None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID") or "")
        self.offset = 0
        self._fetch = fetch or self._http_get

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _http_get(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode())

    def poll(self) -> list[str]:
        """New commands from the configured chat, oldest first."""
        if not self.enabled:
            return []
        url = (f"https://api.telegram.org/bot{self.token}/getUpdates?"
               + urllib.parse.urlencode({"offset": self.offset, "timeout": 0}))
        try:
            data = self._fetch(url)
        except Exception as e:  # never log the URL: it contains the token
            log.warning("telegram poll failed: %s", type(e).__name__)
            return []
        out = []
        for upd in data.get("result", []):
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                continue
            cmd = (msg.get("text") or "").strip().split()[0:1]
            if cmd and cmd[0].split("@")[0].lower() in COMMANDS:
                out.append(cmd[0].split("@")[0].lower())
        return out
