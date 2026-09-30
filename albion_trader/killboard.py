"""Мета по киллборду: последние убийства из официального API игры.

API — тот же, на котором работает киллборд на сайте Albion Online
(``/api/gameinfo/events``). Загрузка выключена по умолчанию и включается
пользователем: это единственный внешний запрос приложения к серверам игры.
Хранятся только нужные поля (снаряжение, сила, число участников) за 7 дней.

Сводка: популярные билды (оружие + броня + голова + обувь без тира и
зачарования) с числом убийств и смертей, популярные предметы по слотам и
«спрос на замену» — какие предметы чаще всего теряют, с текущей ценой.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable

log = logging.getLogger("albion_trader.killboard")

REGIONS = {
    "europe": ("Европа", "https://gameinfo-ams.albiononline.com"),
    "americas": ("Америка", "https://gameinfo.albiononline.com"),
    "asia": ("Азия", "https://gameinfo-sgp.albiononline.com"),
}
PAGE_SIZE = 51                 # больше API не отдаёт за раз
MAX_OFFSET = 1000              # дальше API отвечает ошибкой
PAGES_PER_FETCH = 10
PAGE_PAUSE = 1.5               # пауза между страницами, чтобы не нагружать API
POLL_INTERVAL = 5 * 60
KEEP_DAYS = 7
USER_AGENT = "AlbionTrader (personal market tool)"

SLOTS = {"MainHand": "mainhand", "OffHand": "offhand", "Head": "head", "Armor": "armor", "Shoes": "shoes",
         "Cape": "cape", "Bag": "bag", "Mount": "mount", "Food": "food", "Potion": "potion"}
BUILD_SLOTS = ("mainhand", "offhand", "armor", "head", "shoes")
_TIER = re.compile(r"^T\d_")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_events (
    event_id     INTEGER PRIMARY KEY,
    ts           INTEGER NOT NULL,
    region       TEXT    NOT NULL,
    participants INTEGER,
    fame         INTEGER,
    killer       TEXT    NOT NULL,
    victim       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_ts ON kb_events(region, ts);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)


def base_item(item_id: str) -> str:
    """T8_2H_BOW@2 → 2H_BOW: предмет без тира и зачарования."""
    return _TIER.sub("", (item_id or "").split("@", 1)[0])


def parse_time(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    s = value.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        dt = datetime.fromisoformat(s + "+00:00")
    except ValueError:
        return None
    return int(dt.timestamp())


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _player(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    eq = {}
    for key, slot in SLOTS.items():
        it = (raw.get("Equipment") or {}).get(key) if isinstance(raw.get("Equipment"), dict) else None
        if isinstance(it, dict) and isinstance(it.get("Type"), str) and it["Type"]:
            eq[slot] = [it["Type"][:80], int(_num(it.get("Quality"), 1)) or 1]
    return {"name": str(raw.get("Name") or "")[:40], "guild": str(raw.get("GuildName") or "")[:60],
            "ip": round(_num(raw.get("AverageItemPower")), 1), "eq": eq}


def parse_event(raw, region: str) -> dict | None:
    """Событие API → компактная запись или None, если оно неполное."""
    if not isinstance(raw, dict):
        return None
    try:
        event_id = int(raw.get("EventId"))
    except (TypeError, ValueError):
        return None
    ts = parse_time(raw.get("TimeStamp"))
    killer, victim = _player(raw.get("Killer")), _player(raw.get("Victim"))
    if ts is None or not killer or not victim:
        return None
    participants = raw.get("numberOfParticipants")
    if participants is None and isinstance(raw.get("Participants"), list):
        participants = len(raw["Participants"])
    return {"event_id": event_id, "ts": ts, "region": region, "participants": int(_num(participants, 1)) or 1,
            "fame": int(_num(raw.get("TotalVictimKillFame"))), "killer": killer, "victim": victim}


def store(conn, events: list[dict]) -> int:
    """Сохраняет новые события; возвращает число новых."""
    new = 0
    for e in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO kb_events(event_id, ts, region, participants, fame, killer, victim) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (e["event_id"], e["ts"], e["region"], e["participants"], e["fame"],
             json.dumps(e["killer"], ensure_ascii=False), json.dumps(e["victim"], ensure_ascii=False)))
        new += cur.rowcount
    return new


def cleanup(conn, now: int | None = None) -> int:
    now = int(now or time.time())
    return conn.execute("DELETE FROM kb_events WHERE ts < ?", (now - KEEP_DAYS * 86400,)).rowcount


def signature(eq: dict) -> tuple | None:
    """Билд без тиров: (оружие, вторая рука, броня, голова, обувь). Без оружия — не билд."""
    if "mainhand" not in eq:
        return None
    return tuple(base_item(eq[s][0]) if s in eq else "" for s in BUILD_SLOTS)


def meta_report(conn, region: str, since: int, min_ip: float = 0, mode: str = "all",
                value_of: Callable[[str], float | None] | None = None, min_count: int = 2) -> dict:
    sql = "SELECT * FROM kb_events WHERE region = ? AND ts >= ?"
    args: list = [region, since]
    if mode == "solo":
        sql += " AND participants <= 1"
    elif mode == "group":
        sql += " AND participants >= 2"
    builds: dict[tuple, dict] = {}
    slot_items: dict[str, Counter] = defaultdict(Counter)
    lost: Counter = Counter()
    events = 0
    for r in conn.execute(sql + " ORDER BY ts DESC", args):
        events += 1
        for role, raw in (("kill", r["killer"]), ("death", r["victim"])):
            p = json.loads(raw)
            if p["ip"] < min_ip:
                continue
            for slot, (item, _q) in p["eq"].items():
                slot_items[slot][base_item(item)] += 1
                if role == "death":
                    lost[item] += 1
            sig = signature(p["eq"])
            if not sig:
                continue
            b = builds.setdefault(sig, {"kills": 0, "deaths": 0, "ip_sum": 0.0, "n": 0, "example": None,
                                        "example_role": None})
            b["kills" if role == "kill" else "deaths"] += 1
            b["ip_sum"] += p["ip"]
            b["n"] += 1
            # Пример для конструктора: самое свежее убийство этим билдом, иначе самая свежая смерть.
            if b["example"] is None or (role == "kill" and b["example_role"] != "kill"):
                b["example"], b["example_role"] = p["eq"], role
    rows = []
    for sig, b in builds.items():
        total = b["kills"] + b["deaths"]
        if total < min_count:
            continue
        rows.append({"signature": dict(zip(BUILD_SLOTS, sig)), "kills": b["kills"], "deaths": b["deaths"],
                     "total": total, "win_rate": round(b["kills"] / total * 100, 1),
                     "avg_ip": round(b["ip_sum"] / b["n"]) if b["n"] else None,
                     "example": {slot: {"item": item, "quality": q} for slot, (item, q) in b["example"].items()}})
    rows.sort(key=lambda r: (r["total"], r["kills"]), reverse=True)
    popular = {slot: [{"base": base, "count": n} for base, n in c.most_common(10)] for slot, c in slot_items.items()}
    demand = []
    for item, n in lost.most_common(60):
        price = value_of(item) if value_of else None
        demand.append({"item_id": item, "lost": n, "price": price,
                       "turnover": round(price * n) if price is not None else None})
    return {"events": events, "builds": rows[:100], "popular": popular, "demand": demand}


# --- загрузка ----------------------------------------------------------------

def fetch_page(region: str, offset: int, urlopen=urllib.request.urlopen, timeout: float = 30) -> list:
    base = REGIONS[region][1]
    req = urllib.request.Request(f"{base}/api/gameinfo/events?limit={PAGE_SIZE}&offset={offset}",
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise ValueError("неожиданный ответ API")
    return data


class KillboardFetcher:
    """Фоновая загрузка: раз в POLL_INTERVAL, пока включено в настройках."""

    def __init__(self, conn_factory, write_lock, settings: Callable[[], dict], urlopen=urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.conn_factory = conn_factory
        self.write_lock = write_lock
        self.settings = settings
        self.urlopen = urlopen
        self.sleep = sleep
        self.clock = clock
        self.status = {"running": False, "last_fetch_at": None, "last_new": 0, "error": None}
        self._wake = threading.Event()
        self._busy = threading.Lock()
        self._stop = threading.Event()

    def fetch_once(self, region: str) -> int:
        """Страницы с начала, пока не встретятся уже известные события."""
        if region not in REGIONS:
            raise ValueError("неизвестный сервер")
        if not self._busy.acquire(blocking=False):
            return 0
        total_new = 0
        try:
            self.status.update(running=True, error=None)
            for page in range(PAGES_PER_FETCH):
                offset = page * PAGE_SIZE
                if offset > MAX_OFFSET:
                    break
                raw = fetch_page(region, offset, self.urlopen)
                events = [e for e in (parse_event(x, region) for x in raw) if e]
                with self.write_lock, self.conn_factory() as conn:
                    new = store(conn, events)
                    if page == 0:
                        cleanup(conn, int(self.clock()))
                total_new += new
                if not raw or new < len(events):   # дошли до уже загруженного
                    break
                self.sleep(PAGE_PAUSE)
            self.status.update(last_fetch_at=int(self.clock()), last_new=total_new)
            return total_new
        except (OSError, ValueError) as e:
            self.status["error"] = str(e)[:200]
            log.warning("Киллборд: %s", e)
            raise
        finally:
            self.status["running"] = False
            self._busy.release()

    def fetch_async(self) -> None:
        self._wake.set()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="killboard").start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:  # pragma: no cover - фоновый цикл, логика — в fetch_once
        while not self._stop.is_set():
            s = self.settings()
            forced = self._wake.is_set()
            self._wake.clear()
            if s.get("killboard_enabled") or forced:
                try:
                    self.fetch_once(s.get("killboard_region") or "europe")
                except (OSError, ValueError):
                    pass
            self._wake.wait(POLL_INTERVAL)
