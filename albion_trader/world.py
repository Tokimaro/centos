"""События мира: нападение бандитов (RedZoneWorldMapEvent) и фестивали
(FestivitiesUpdate). Время в событиях — тики .NET.

RedZoneWorldMapEvent: 0 — время окончания фазы, 1 — фаза (1–3), 2 — провинции 3-й фазы.
FestivitiesUpdate: 0 — виды, 1 — категории, 2 — названия, 3 — начало, 4 — конец.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Callable

from .db import dotnet_ticks_to_unix

log = logging.getLogger("albion_trader.world")

SCHEMA = """
CREATE TABLE IF NOT EXISTS world_events (
    kind     TEXT    NOT NULL,
    name     TEXT    NOT NULL,
    phase    INTEGER,
    start_ts INTEGER,
    end_ts   INTEGER NOT NULL,
    data     TEXT,
    updated  INTEGER NOT NULL,
    PRIMARY KEY (kind, name, end_ts)
);
"""

BANDIT_PHASES = {1: "объявлено", 2: "подготовка", 3: "нападение идёт"}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _ts(v) -> int | None:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return dotnet_ticks_to_unix(v) if v > 10**14 else v


class World:
    def __init__(self, conn_factory: Callable, write_lock, alert: Callable | None = None,
                 clock: Callable[[], float] = time.time):
        self.conn_factory = conn_factory
        self.write_lock = write_lock
        self.alert = alert          # alert(conn, key, title, text, payload)
        self.clock = clock

    def attach(self, state) -> None:
        state.on("event:redzone_world_map_event", self.on_bandit)
        state.on("event:festivities_update", self.on_festivities)

    def _save(self, conn, kind, name, phase, start, end, data) -> bool:
        cur = conn.execute("SELECT phase FROM world_events WHERE kind=? AND name=? AND end_ts=?", (kind, name, end))
        row = cur.fetchone()
        conn.execute("""INSERT OR REPLACE INTO world_events(kind, name, phase, start_ts, end_ts, data, updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (kind, name, phase, start, end, json.dumps(data, ensure_ascii=False), int(self.clock())))
        return row is None or row[0] != phase

    def on_bandit(self, p: dict) -> None:
        end = _ts(p.get(0))
        phase = p.get(1) if isinstance(p.get(1), int) else None
        if not end:
            return
        provinces = p.get(2) if isinstance(p.get(2), list) else []
        with self.write_lock, self.conn_factory() as conn:
            changed = self._save(conn, "bandit", "bandit", phase, None, end, {"provinces": provinces})
            if changed and self.alert:
                left = max(0, end - int(self.clock()))
                self.alert(conn, f"bandit:{end}:{phase}", "Нападение бандитов",
                           f"Фаза {phase} ({BANDIT_PHASES.get(phase, '?')}), до конца фазы {left // 60} мин"
                           + (f"; провинции: {', '.join(provinces)}" if provinces else ""),
                           {"end": end, "phase": phase})
        log.info("Нападение бандитов: фаза %s до %s", phase, end)

    def on_festivities(self, p: dict) -> None:
        names = p.get(2) if isinstance(p.get(2), list) else []
        cats = p.get(1) if isinstance(p.get(1), list) else []
        starts = p.get(3) if isinstance(p.get(3), list) else []
        ends = p.get(4) if isinstance(p.get(4), list) else []
        if not names or not (len(names) == len(starts) == len(ends)):
            return
        now = int(self.clock())
        with self.write_lock, self.conn_factory() as conn:
            for i, name in enumerate(names):
                start, end = _ts(starts[i]), _ts(ends[i])
                if not name or not end:
                    continue
                cat = cats[i] if i < len(cats) else ""
                if self._save(conn, "festival", str(name), None, start, end, {"category": cat}) and self.alert \
                        and end > now:
                    self.alert(conn, f"festival:{name}:{start}", f"Фестиваль: {name}",
                               f"{cat or 'событие'}: " + ("идёт" if start and start <= now else "скоро начнётся"),
                               {"start": start, "end": end})


def report(conn, now: int) -> dict:
    bandit = conn.execute(
        "SELECT * FROM world_events WHERE kind='bandit' ORDER BY end_ts DESC LIMIT 1").fetchone()
    festivals = [dict(r) for r in conn.execute(
        "SELECT * FROM world_events WHERE kind='festival' AND end_ts >= ? ORDER BY start_ts", (now - 86400,))]
    for f in festivals:
        f["data"] = json.loads(f["data"] or "{}")
    b = dict(bandit) if bandit else None
    if b:
        b["data"] = json.loads(b["data"] or "{}")
        b["phase_name"] = BANDIT_PHASES.get(b["phase"], "")
    return {"bandit": b, "festivals": festivals}
