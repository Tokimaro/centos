"""Личная активность: сессии (слава, серебро, лут в час), журнал лута группы,
сведения о персонаже.

События игры (номера сверены по albiondata-client и StatisticsAnalysisTool;
значения «с фиксированной точкой» делятся на 10 000):

* UpdateFame: 1 — вся слава персонажа, 2 — полученная с множителем зоны;
* TakeSilver: 3 — серебро до налогов, 4 — налог кластера, 5 — налог гильдии;
* UpdateMoney: 1 — баланс серебра;
* OtherGrabbedLoot: 1 — у кого, 2 — кто подобрал, 3 — это серебро, 4 — индекс
  предмета, 5 — количество;
* Died: 2 — погибший, 10 — убийца; KilledPlayer: 2 — убитый игрок;
* UpdateReSpecPoints: 0 — массив очков (индекс 1 — текущие), 2 — получено;
* CharacterStats: сохраняется как есть.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict
from typing import Callable

log = logging.getLogger("albion_trader.activity")

FIXPOINT = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   INTEGER NOT NULL,
    ended     INTEGER,
    character TEXT
);
CREATE TABLE IF NOT EXISTS activity_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    session_id INTEGER,
    kind       TEXT    NOT NULL,
    location   TEXT,
    item_id    TEXT,
    amount     REAL,
    value      REAL,
    actor      TEXT,
    target     TEXT,
    data       TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_session ON activity_events(session_id, kind);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_events(ts, kind);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _fix(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v) / FIXPOINT
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Activity:
    def __init__(self, conn_factory: Callable, write_lock, state=None, index_to_item: dict | None = None,
                 clock: Callable[[], float] = time.time):
        self.conn_factory = conn_factory
        self.write_lock = write_lock
        self.state = state
        self.index = index_to_item or {}
        self.clock = clock
        self.session_id: int | None = None

    # --- сессии ---------------------------------------------------------
    def new_session(self) -> int:
        now = int(self.clock())
        with self.write_lock, self.conn_factory() as conn:
            conn.execute("UPDATE sessions SET ended = ? WHERE ended IS NULL", (now,))
            cur = conn.execute("INSERT INTO sessions(started, character) VALUES (?, ?)",
                               (now, getattr(self.state, "character_name", "") or None))
            self.session_id = cur.lastrowid
        return self.session_id

    def attach(self, state) -> None:
        self.state = state
        state.on("event:update_fame", self.on_fame)
        state.on("event:take_silver", self.on_silver)
        state.on("event:update_money", self.on_money)
        state.on("event:other_grabbed_loot", self.on_loot)
        state.on("event:died", self.on_died)
        state.on("event:killed_player", self.on_killed)
        state.on("event:update_respec_points", self.on_respec)
        state.on("event:character_stats", self.on_stats)
        state.on("location", self.on_zone)

    def record(self, kind: str, **fields) -> None:
        if self.session_id is None:
            self.new_session()
        loc = getattr(self.state, "location", None) if self.state else None
        with self.write_lock, self.conn_factory() as conn:
            conn.execute(
                """INSERT INTO activity_events(ts, session_id, kind, location, item_id, amount, value, actor,
                                               target, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(self.clock()), self.session_id, kind, loc, fields.get("item_id"), fields.get("amount"),
                 fields.get("value"), fields.get("actor"), fields.get("target"),
                 json.dumps(fields["data"], ensure_ascii=False, default=str) if "data" in fields else None))

    # --- обработчики событий --------------------------------------------
    def on_fame(self, p: dict) -> None:
        gained = _fix(p.get(2))
        if gained is None:
            gained = _fix(p.get(3))
        self.record("fame", value=gained or 0, amount=_fix(p.get(1)))

    def on_silver(self, p: dict) -> None:
        gross = _fix(p.get(3)) or 0
        net = gross - (_fix(p.get(4)) or 0) - (_fix(p.get(5)) or 0)
        self.record("silver", value=round(net, 2), amount=gross)

    def on_money(self, p: dict) -> None:
        balance = _fix(p.get(1))
        if balance is not None:
            self.record("balance", value=balance)

    def on_loot(self, p: dict) -> None:
        is_silver = bool(p.get(3))
        index = _int(p.get(4))
        qty = _int(p.get(5)) or 1
        item = None if is_silver else self.index.get(str(index)) if index is not None else None
        self.record("loot", item_id=item if item else (None if is_silver else f"#{index}"),
                    amount=qty, value=qty if is_silver else None,
                    actor=str(p.get(2) or ""), target=str(p.get(1) or ""), data={"silver": is_silver})

    def on_died(self, p: dict) -> None:
        self.record("death", actor=str(p.get(10) or ""), target=str(p.get(2) or ""))

    def on_killed(self, p: dict) -> None:
        self.record("kill", target=str(p.get(2) or ""))

    def on_respec(self, p: dict) -> None:
        points = p.get(0)
        current = _fix(points[1]) if isinstance(points, list) and len(points) > 1 else None
        self.record("respec", value=current, amount=_fix(p.get(2)))

    def on_stats(self, p: dict) -> None:
        self.record("stats", data={str(k): v for k, v in p.items() if k not in (252,)})

    def on_zone(self, loc: str) -> None:
        self.record("zone", target=loc)


# --- отчёты ---------------------------------------------------------------

def session_report(conn, session_id: int, character: str, value_of: Callable[[str], float | None],
                   now: int) -> dict:
    s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not s:
        return {}
    s = dict(s)
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM activity_events WHERE session_id = ? ORDER BY ts", (session_id,))]
    end = s["ended"] or now
    hours = max((end - s["started"]) / 3600, 1 / 60)
    fame = sum(e["value"] or 0 for e in events if e["kind"] == "fame")
    silver = sum(e["value"] or 0 for e in events if e["kind"] == "silver")
    balances = [e["value"] for e in events if e["kind"] == "balance" and e["value"] is not None]
    me = character or s.get("character") or ""
    loot_items: dict[str, dict] = defaultdict(lambda: {"amount": 0, "value": 0.0, "priced": True})
    loot_silver = 0.0
    for e in events:
        if e["kind"] != "loot" or (me and e["actor"] != me):
            continue
        data = json.loads(e["data"] or "{}")
        if data.get("silver"):
            loot_silver += e["amount"] or 0
            continue
        it = loot_items[e["item_id"] or "?"]
        it["amount"] += e["amount"] or 0
    loot_value = 0.0
    items = []
    for item_id, it in loot_items.items():
        price = value_of(item_id) if item_id and not item_id.startswith("#") else None
        value = price * it["amount"] if price is not None else None
        loot_value += value or 0
        items.append({"item_id": item_id, "amount": it["amount"], "unit_value": price, "value": value})
    zones: dict[str, dict] = {}
    current = None
    for e in events:
        if e["kind"] == "zone":
            current = e["target"]
        key = e["location"] or current or "?"
        z = zones.setdefault(key, {"location": key, "fame": 0.0, "silver": 0.0, "first": e["ts"], "last": e["ts"]})
        z["last"] = e["ts"]
        if e["kind"] == "fame":
            z["fame"] += e["value"] or 0
        elif e["kind"] == "silver":
            z["silver"] += e["value"] or 0
    return {
        "session": s, "hours": round(hours, 3), "character": me,
        "fame": round(fame), "fame_per_hour": round(fame / hours),
        "silver": round(silver), "silver_per_hour": round(silver / hours),
        "loot_silver": round(loot_silver),
        "loot_value": round(loot_value), "loot_value_per_hour": round(loot_value / hours),
        "balance_start": balances[0] if balances else None, "balance_end": balances[-1] if balances else None,
        "balance_change": round(balances[-1] - balances[0]) if len(balances) > 1 else None,
        "deaths": sum(1 for e in events if e["kind"] == "death" and me and e["target"] == me),
        "kills": sum(1 for e in events if e["kind"] == "kill"),
        "items": sorted(items, key=lambda i: i["value"] or 0, reverse=True),
        "zones": sorted(zones.values(), key=lambda z: z["first"]),
        "events": len(events),
    }


def loot_log(conn, since: int, session_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM activity_events WHERE kind = 'loot' AND ts >= ?"
    args: list = [since]
    if session_id:
        sql += " AND session_id = ?"
        args.append(session_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY ts DESC", args)]


def character_report(conn, since: int) -> dict:
    fame_days: dict[str, float] = defaultdict(float)
    fame_total = []
    balance = []
    respec = None
    deaths = kills = 0
    stats = None
    for e in conn.execute("SELECT * FROM activity_events WHERE ts >= ? ORDER BY ts", (since,)):
        e = dict(e)
        day = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
        if e["kind"] == "fame":
            fame_days[day] += e["value"] or 0
            if e["amount"]:
                fame_total.append([e["ts"], e["amount"]])
        elif e["kind"] == "balance" and e["value"] is not None:
            balance.append([e["ts"], e["value"]])
        elif e["kind"] == "respec" and e["value"] is not None:
            respec = {"ts": e["ts"], "points": e["value"]}
        elif e["kind"] == "death":
            deaths += 1
        elif e["kind"] == "kill":
            kills += 1
        elif e["kind"] == "stats":
            stats = {"ts": e["ts"], "data": json.loads(e["data"] or "{}")}
    return {"fame_days": [{"day": d, "fame": round(v)} for d, v in sorted(fame_days.items(), reverse=True)],
            "fame_total": fame_total, "balance": balance, "respec": respec, "deaths": deaths,
            "kills": kills, "stats": stats}
