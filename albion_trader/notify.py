"""Пересылка оповещений в Telegram и Discord (только если пользователь сам
указал свой бот/вебхук). Отправка идёт в фоновом потоке с ограничением частоты.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

log = logging.getLogger("albion_trader.notify")

MIN_INTERVAL = 2.0     # не чаще одного сообщения в 2 с на канал
QUEUE_LIMIT = 100


def _default_opener(url: str, payload: dict, timeout: float = 15) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class Notifier:
    def __init__(self, settings: Callable[[], dict], opener: Callable = _default_opener,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.opener = opener
        self.clock = clock
        self.sleep = sleep
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self.last_sent = 0.0
        self.stats = {"sent": 0, "errors": 0, "dropped": 0, "last_error": None}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="notify")
            self._thread.start()

    def channels(self, s: dict | None = None) -> list[tuple[str, str, dict]]:
        """[(имя, url, шаблон полезной нагрузки)] для включённых каналов."""
        s = s or self.settings()
        out = []
        token, chat = (s.get("telegram_token") or "").strip(), str(s.get("telegram_chat_id") or "").strip()
        if s.get("telegram_enabled") and token and chat:
            out.append(("telegram", f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat}))
        hook = (s.get("discord_webhook") or "").strip()
        if s.get("discord_enabled") and hook.startswith("https://"):
            out.append(("discord", hook, {}))
        return out

    @staticmethod
    def format(alert: dict) -> str:
        return f"{alert.get('title', '')}\n{alert.get('text', '')}".strip()

    def enqueue(self, alert: dict) -> None:
        s = self.settings()
        kinds = s.get("notify_kinds") or []
        if kinds and alert.get("kind") not in kinds:
            return
        if not self.channels(s):
            return
        try:
            self.queue.put_nowait(alert)
        except queue.Full:
            self.stats["dropped"] += 1

    def send_now(self, text: str) -> list[dict]:
        """Отправка сразу во все каналы (для кнопки «Проверить»). Возвращает результат по каналам."""
        results = []
        for name, url, base in self.channels():
            payload = dict(base, text=text) if name == "telegram" else {"content": text}
            try:
                status = self.opener(url, payload)
                results.append({"channel": name, "ok": 200 <= status < 300, "status": status})
            except (urllib.error.URLError, OSError, ValueError) as e:
                results.append({"channel": name, "ok": False, "error": str(e)})
        return results

    def _loop(self) -> None:
        while True:
            alert = self.queue.get()
            wait = MIN_INTERVAL - (self.clock() - self.last_sent)
            if wait > 0:
                self.sleep(wait)
            for res in self.send_now(self.format(alert)):
                if res["ok"]:
                    self.stats["sent"] += 1
                else:
                    self.stats["errors"] += 1
                    self.stats["last_error"] = res.get("error") or f"HTTP {res.get('status')}"
                    log.warning("Не удалось отправить оповещение в %s: %s", res["channel"], self.stats["last_error"])
            self.last_sent = self.clock()
