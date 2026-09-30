"""Личная активность: сессии (слава, серебро, лут в час), журнал лута группы,
сведения о персонаже.

События игры (номера сверены по albiondata-client и StatisticsAnalysisTool;
значения «с фиксированной точкой» делятся на 10 000):

* UpdateFame: 1 — вся слава персонажа, 2 — полученная с множителем зоны (без
  премиума), 5 — премиум (+50 %), 10 — слава сумки прозрения, 17 — бонус-фактор
  (дробное число, не фиксированная точка). Получено = (2 + премиум + 10) × (1 + 17);
* источник славы — событие завершения рядом по времени: HarvestFinished (сбор),
  CraftItemFinished (крафт), FishingFinished (рыбалка), иначе бой;
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
FAME_WINDOW = 3.0     # секунды: слава сразу после сбора/крафта/улова — от них
FAME_SOURCES = {"combat": "бой", "gathering": "сбор", "crafting": "крафт", "fishing": "рыбалка"}

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
        self.last_finished: dict[str, float] = {}   # источник славы -> время завершения действия

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
        state.on("event:harvest_finished", lambda p: self._finished("gathering", p))
        state.on("event:craft_item_finished", lambda p: self._finished("crafting", p))
        state.on("event:fishing_finished", lambda p: self._finished("fishing", p))
        state.on("location", self.on_zone)

    def record(self, kind: str, **fields) -> int:
        if self.session_id is None:
            self.new_session()
        loc = getattr(self.state, "location", None) if self.state else None
        with self.write_lock, self.conn_factory() as conn:
            return conn.execute(
                """INSERT INTO activity_events(ts, session_id, kind, location, item_id, amount, value, actor,
                                               target, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(self.clock()), self.session_id, kind, loc, fields.get("item_id"), fields.get("amount"),
                 fields.get("value"), fields.get("actor"), fields.get("target"),
                 json.dumps(fields["data"], ensure_ascii=False, default=str) if "data" in fields else None)).lastrowid

    # --- обработчики событий --------------------------------------------
    def is_mine(self, p: dict, key: int = 0) -> bool:
        """Событие своего персонажа: id объекта совпадает с Join[0] (если оба известны)."""
        own = getattr(self.state, "object_id", None) if self.state else None
        who = _int(p.get(key))
        return own is None or who is None or who == own

    def _finished(self, source: str, p: dict) -> None:
        if not self.is_mine(p):
            return
        now = self.clock()
        self.last_finished[source] = now
        # Слава могла прийти чуть раньше события завершения — уточняем её источник.
        last = getattr(self, "_last_fame", None)
        if last and last[2] == "combat" and now - last[1] <= FAME_WINDOW:
            self._last_fame = (last[0], last[1], source)
            with self.write_lock, self.conn_factory() as conn:
                row = conn.execute("SELECT data FROM activity_events WHERE id = ?", (last[0],)).fetchone()
                if row:
                    data = json.loads(row[0] or "{}")
                    data["src"] = source
                    conn.execute("UPDATE activity_events SET data = ? WHERE id = ?",
                                 (json.dumps(data, ensure_ascii=False), last[0]))

    def fame_source(self) -> str:
        now = self.clock()
        recent = [(ts, src) for src, ts in self.last_finished.items() if now - ts <= FAME_WINDOW]
        return max(recent)[1] if recent else "combat"

    def on_fame(self, p: dict) -> None:
        base = _fix(p.get(2))
        if base is None:
            base = _fix(p.get(3)) or 0.0
        premium = base * 0.5 if p.get(5) is True else 0.0
        satchel = _fix(p.get(10)) or 0.0
        bonus = p.get(17)
        bonus = float(bonus) if isinstance(bonus, (int, float)) and not isinstance(bonus, bool) \
            and 0 <= bonus < 100 else 0.0
        total = (base + premium + satchel) * (1 + bonus)
        src = self.fame_source()
        rowid = self.record("fame", value=round(total, 2), amount=_fix(p.get(1)),
                            data={"src": src, "base": round(base, 2), "premium": round(premium, 2),
                                  "satchel": round(satchel, 2), "bonus": round(bonus, 4)})
        self._last_fame = (rowid, self.clock(), src)

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
        # 2 — погибший, 3 — его гильдия, 10 — убийца, 11 — его гильдия.
        self.record("death", actor=str(p.get(10) or ""), target=str(p.get(2) or ""),
                    data={"victim_guild": str(p.get(3) or ""), "killer_guild": str(p.get(11) or "")})

    def on_killed(self, p: dict) -> None:
        me = getattr(self.state, "character_name", "") if self.state else ""
        self.record("kill", actor=me, target=str(p.get(2) or ""))

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


def kill_log(conn, since: int, character: str, session_id: int | None = None) -> dict:
    """Журнал смертей рядом с вами (Died) и ваших убийств (KilledPlayer).

    Одна и та же смерть может прийти обоими событиями — убийство из KilledPlayer
    не дублируется, если в ту же секунду есть Died с той же жертвой.
    """
    sql = "SELECT * FROM activity_events WHERE kind IN ('death', 'kill') AND ts >= ?"
    args: list = [since]
    if session_id:
        sql += " AND session_id = ?"
        args.append(session_id)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY ts DESC", args)]
    deaths = {(r["ts"], r["target"]) for r in rows if r["kind"] == "death"}
    out = []
    for r in rows:
        if r["kind"] == "kill" and ((r["ts"], r["target"]) in deaths or (r["ts"] - 1, r["target"]) in deaths):
            continue
        data = json.loads(r["data"] or "{}")
        killer = r["actor"] or (character if r["kind"] == "kill" else "")
        out.append({"ts": r["ts"], "location": r["location"], "victim": r["target"], "killer": killer,
                    "victim_guild": data.get("victim_guild", ""), "killer_guild": data.get("killer_guild", ""),
                    "mine": bool(character) and character in (killer, r["target"]),
                    "my_death": bool(character) and r["target"] == character,
                    "my_kill": bool(character) and killer == character})
    killers_of_me: dict[str, int] = defaultdict(int)
    for r in out:
        if r["my_death"] and r["killer"]:
            killers_of_me[r["killer"]] += 1
    return {"rows": out,
            "my_kills": sum(1 for r in out if r["my_kill"]),
            "my_deaths": sum(1 for r in out if r["my_death"]),
            "seen": len(out),
            "top_killers": sorted(({"player": k, "count": v} for k, v in killers_of_me.items()),
                                  key=lambda x: x["count"], reverse=True)[:10]}


# Если в зоне долго нет событий, считаем, что вы отошли от игры: время
# между событиями учитывается не больше этого.
IDLE_GAP = 10 * 60


def zone_report(conn, since: int, character: str, value_of: Callable[[str], float | None]) -> list[dict]:
    """Сводка по зонам за период: время, визиты, слава, серебро, лут (ваш), смерти, в час."""
    zones: dict[str, dict] = {}

    def zone(key):
        return zones.setdefault(key, {"location": key, "seconds": 0, "visits": 0, "fame": 0.0, "silver": 0.0,
                                      "loot_items": defaultdict(float), "loot_silver": 0.0, "deaths": 0,
                                      "kills": 0, "last": 0})
    prev = None           # (сессия, зона, время) предыдущего события
    for e in conn.execute("SELECT * FROM activity_events WHERE ts >= ? ORDER BY session_id, ts, id", (since,)):
        e = dict(e)
        key = e["target"] if e["kind"] == "zone" else e["location"]
        if not key:
            continue
        z = zone(key)
        if prev and prev[0] == e["session_id"]:
            gap = e["ts"] - prev[2]
            zone(prev[1])["seconds"] += min(max(gap, 0), IDLE_GAP)
        if e["kind"] == "zone" and (not prev or prev[1] != key or prev[0] != e["session_id"]):
            z["visits"] += 1
        z["last"] = max(z["last"], e["ts"])
        if e["kind"] == "fame":
            z["fame"] += e["value"] or 0
        elif e["kind"] == "silver":
            z["silver"] += e["value"] or 0
        elif e["kind"] == "loot" and (not character or e["actor"] == character):
            if json.loads(e["data"] or "{}").get("silver"):
                z["loot_silver"] += e["amount"] or 0
            elif e["item_id"]:
                z["loot_items"][e["item_id"]] += e["amount"] or 0
        elif e["kind"] == "death" and character and e["target"] == character:
            z["deaths"] += 1
        elif e["kind"] == "kill":
            z["kills"] += 1
        prev = (e["session_id"], key, e["ts"])
    out = []
    for z in zones.values():
        loot_value = 0.0
        unpriced = 0
        for item_id, amount in z.pop("loot_items").items():
            price = value_of(item_id) if not item_id.startswith("#") else None
            if price is None:
                unpriced += 1
            else:
                loot_value += price * amount
        hours = z["seconds"] / 3600
        income = z["silver"] + z["loot_silver"] + loot_value
        z.update({
            "hours": round(hours, 2), "loot_value": round(loot_value), "unpriced_items": unpriced,
            "fame": round(z["fame"]), "silver": round(z["silver"]), "loot_silver": round(z["loot_silver"]),
            "income": round(income),
            "fame_per_hour": round(z["fame"] / hours) if hours >= 1 / 60 else None,
            "income_per_hour": round(income / hours) if hours >= 1 / 60 else None,
        })
        if z["fame"] or z["silver"] or z["loot_value"] or z["loot_silver"] or z["hours"] or z["deaths"] or z["kills"]:
            out.append(z)
    out.sort(key=lambda z: z["income_per_hour"] or 0, reverse=True)
    return out
