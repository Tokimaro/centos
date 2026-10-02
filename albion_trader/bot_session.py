"""Запись сессий бота для разбора ошибок: трафик окна игры и журнал действий.

Папка ``data/bot_sessions/<время>-<задача>/``:

* ``traffic.bin`` — пакеты игры этого окна: заголовок ``<dHI`` (время, локальный
  порт, длина) и полезная нагрузка UDP;
* ``log.jsonl`` — что бот делал: клики (доли окна), клавиши, ввод, записи журнала.

``python -m albion_trader bot-session ПАПКА`` показывает сводку: зоны по времени,
какие события и объекты приходили (в том числе с неизвестными кодами — так
находятся номера событий своего сервера), что бот делал и чем закончилось. Ту же
запись можно прогнать через модель игры в тестах.
"""

from __future__ import annotations

import collections
import json
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

HEADER = struct.Struct("<dHI")
MAX_BYTES = 200 * 1024 * 1024        # одна сессия не больше 200 МБ трафика


class Session:
    def __init__(self, root: str | Path, task: str, clock: Callable[[], float] = time.time):
        self.clock = clock
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        self.dir = Path(root) / f"{stamp}-{task}"
        n = 1
        while self.dir.exists():
            n += 1
            self.dir = Path(root) / f"{stamp}-{task}-{n}"
        self.dir.mkdir(parents=True)
        self.lock = threading.Lock()
        self._traffic = open(self.dir / "traffic.bin", "wb")
        self._log = open(self.dir / "log.jsonl", "w", encoding="utf-8")
        self.bytes = 0
        self.log({"a": "start", "task": task})

    def packet(self, port: int, payload: bytes) -> None:
        with self.lock:
            if self._traffic.closed or self.bytes > MAX_BYTES:
                return
            self._traffic.write(HEADER.pack(self.clock(), port & 0xFFFF, len(payload)))
            self._traffic.write(payload)
            self.bytes += HEADER.size + len(payload)

    def log(self, entry: dict) -> None:
        with self.lock:
            if self._log.closed:
                return
            self._log.write(json.dumps({"t": round(self.clock(), 2), **entry}, ensure_ascii=False) + "\n")
            self._log.flush()

    def close(self) -> None:
        self.log({"a": "end"})
        with self.lock:
            self._traffic.close()
            self._log.close()


def read_traffic(path: str | Path) -> Iterator[tuple[float, int, bytes]]:
    with open(path, "rb") as f:
        while True:
            head = f.read(HEADER.size)
            if len(head) < HEADER.size:
                return
            ts, port, n = HEADER.unpack(head)
            data = f.read(n)
            if len(data) < n:
                return
            yield ts, port, data


def read_log(path: str | Path) -> list[dict]:
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summarize(folder: str | Path, make_feed: Callable) -> str:
    """Сводка по записанной сессии: зоны, события, объекты, действия бота, журнал."""
    folder = Path(folder)
    feeds: dict[int, object] = {}
    zones: list[tuple[float, str]] = []
    objects: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    first = last = None
    packets = 0
    for ts, port, payload in read_traffic(folder / "traffic.bin"):
        feed = feeds.get(port)
        if feed is None:
            feed = feeds[port] = make_feed()
        before = feed.zone
        try:
            feed.feed(payload)
        except Exception:  # noqa: BLE001 - битый пакет не мешает сводке
            pass
        packets += 1
        first = ts if first is None else first
        last = ts
        if feed.zone and feed.zone != before:
            zones.append((ts, feed.zone))
        for e in feed.entities():
            objects[feed.zone or "?"][f"{e.kind}:{e.event or e.kind}"] += 0
    codes: collections.Counter = collections.Counter()
    requests: collections.Counter = collections.Counter()
    unknown: dict[int, str] = {}
    for feed in feeds.values():
        requests.update(feed.request_counts)
        names = {v: k for k, v in feed.state.ev.items()}
        for code, info in feed.radar.codes.items():
            codes[names.get(code) or f"код {code}"] += info["count"]
            if code not in names:
                unknown[code] = info.get("shape", "")
        for e in feed.entities():
            objects[feed.zone or "?"][f"{e.kind}:{e.event or e.kind}"] += 1
    actions = read_log(folder / "log.jsonl")
    kinds = collections.Counter(a.get("a") for a in actions)
    t0 = first or (actions[0]["t"] if actions else 0)
    lines = [f"Сессия бота: {folder.name}",
             f"Пакетов: {packets}, длительность: {((last or t0) - t0) / 60:.1f} мин",
             "", "Зоны:"]
    lines += [f"  +{(ts - t0) / 60:6.1f} мин  {z}" for ts, z in zones] or ["  (смены зон нет)"]
    lines += ["", "Запросы игры: " + (", ".join(f"{k} {v}" for k, v in requests.most_common(15)) or "нет")]
    lines += ["", "События (чаще всего):"]
    lines += [f"  {n:7d}  {name}" for name, n in codes.most_common(25)] or ["  (нет)"]
    if unknown:
        lines += ["", "Неизвестные коды событий (форма параметров) — кандидаты для data/opcodes.json:"]
        lines += [f"  {code}: {shape[:120]}" for code, shape in sorted(unknown.items())[:30]]
    lines += ["", "Объекты в последней зоне каждого окна:"]
    for zone, cnt in objects.items():
        items = ", ".join(f"{k} × {v}" for k, v in cnt.most_common() if v)
        lines.append(f"  {zone}: {items or '—'}")
    lines += ["", "Действия бота: " + (", ".join(f"{k} {v}" for k, v in kinds.most_common() if k) or "нет")]
    notes = [a for a in actions if a.get("a") == "note"]
    lines += ["", "Журнал (последние 40):"]
    lines += [f"  +{(a['t'] - t0) / 60:6.1f} мин  {a.get('text', '')}" for a in notes[-40:]] or ["  (пусто)"]
    return "\n".join(lines) + "\n"
