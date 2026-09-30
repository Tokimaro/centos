"""Картограф Дорог Авалона: связи между зонами и маршруты.

Порталы Дорог Авалона случайны и живут ограниченное время, поэтому связи
добавляются вручную (зона A ↔ зона B, размер портала, сколько осталось) или
автоматически: когда вы сменили зону и одна из зон — Дорога (``TNL-…`` /
тип ``TUNNEL_*``), это переход через портал. Время жизни в трафике не видно:
у автоматической связи оно неизвестно, пока вы его не укажете, и такая связь
удаляется через сутки.

Маршрут — поиск в ширину по числу переходов: активные связи плюс обычные
переходы карты мира (Королевские земли, Внешние земли).
"""

from __future__ import annotations

import time
from collections import deque

from .gamedata import GameData

AUTO_TTL = 24 * 3600          # автосвязь без указанного времени живёт сутки
SIZES = (0, 2, 7, 20)         # 0 — неизвестно

SCHEMA = """
CREATE TABLE IF NOT EXISTS avalon_links (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    a       TEXT    NOT NULL,
    b       TEXT    NOT NULL,
    size    INTEGER NOT NULL DEFAULT 0,
    expires INTEGER,
    source  TEXT    NOT NULL,
    note    TEXT    NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    seen    INTEGER NOT NULL,
    UNIQUE (a, b)
);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)


def is_road(gd: GameData, zone: str) -> bool:
    return bool(zone) and (zone.startswith("TNL-") or gd.cluster_type(zone).startswith("TUNNEL"))


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def static_neighbors(gd: GameData, zone: str) -> set[str]:
    return set((gd.cluster(zone) or [None, None, []])[2])


def resolve_zone(gd: GameData, text: str) -> str | None:
    """Зона по id или названию (без учёта регистра)."""
    text = (text or "").strip()
    if not text:
        return None
    if text in gd.clusters:
        return text
    low = text.lower()
    for cid, c in gd.clusters.items():
        if (c[0] or "").lower() == low:
            return cid
    if text.startswith("TNL-") or text.isdigit():
        return text
    return None


def search_zones(gd: GameData, query: str, limit: int = 30) -> list[dict]:
    words = (query or "").lower().split()
    out = []
    for cid, (name, ctype, _exits) in gd.clusters.items():
        text = f"{name} {cid}".lower()
        if words and all(w in text for w in words):
            out.append({"id": cid, "name": name, "type": ctype, "road": ctype.startswith("TUNNEL")})
    out.sort(key=lambda z: (not z["road"], z["name"]))
    return out[:limit]


def add_link(conn, a: str, b: str, size: int = 0, hours_left: float | None = None, note: str = "",
             source: str = "manual", now: int | None = None) -> int:
    now = int(now or time.time())
    if not a or not b or a == b:
        raise ValueError("нужны две разные зоны")
    if size not in SIZES:
        raise ValueError("размер портала: 2, 7 или 20")
    a, b = _pair(a, b)
    expires = now + int(hours_left * 3600) if hours_left and hours_left > 0 else None
    row = conn.execute("SELECT id, expires, source FROM avalon_links WHERE a = ? AND b = ?", (a, b)).fetchone()
    if row:
        # Повторный переход по той же связи не затирает то, что вы указали вручную.
        if source == "auto":
            conn.execute("UPDATE avalon_links SET seen = ? WHERE id = ?", (now, row["id"]))
        else:
            conn.execute("UPDATE avalon_links SET size = ?, expires = ?, note = ?, source = ?, seen = ? WHERE id = ?",
                         (size, expires, note[:200], source, now, row["id"]))
        return row["id"]
    return conn.execute(
        "INSERT INTO avalon_links(a, b, size, expires, source, note, created, seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (a, b, size, expires, source, note[:200], now, now)).lastrowid


def update_link(conn, link_id: int, size: int, hours_left: float | None, note: str, now: int | None = None) -> None:
    now = int(now or time.time())
    if size not in SIZES:
        raise ValueError("размер портала: 2, 7 или 20")
    expires = now + int(hours_left * 3600) if hours_left and hours_left > 0 else None
    cur = conn.execute("UPDATE avalon_links SET size = ?, expires = ?, note = ?, source = 'manual' WHERE id = ?",
                       (size, expires, (note or "")[:200], link_id))
    if not cur.rowcount:
        raise ValueError("связь не найдена")


def delete_link(conn, link_id: int) -> None:
    conn.execute("DELETE FROM avalon_links WHERE id = ?", (link_id,))


def cleanup(conn, now: int | None = None) -> int:
    now = int(now or time.time())
    return conn.execute("DELETE FROM avalon_links WHERE (expires IS NOT NULL AND expires < ?) "
                        "OR (expires IS NULL AND source = 'auto' AND seen < ?)", (now, now - AUTO_TTL)).rowcount


def links(conn, now: int | None = None) -> list[dict]:
    now = int(now or time.time())
    out = []
    for r in conn.execute("SELECT * FROM avalon_links ORDER BY COALESCE(expires, seen + ?) ", (AUTO_TTL,)):
        r = dict(r)
        r["left"] = r["expires"] - now if r["expires"] else None
        out.append(r)
    return out


def on_zone_change(conn, gd: GameData, zone: str, prev: str, now: int | None = None) -> int | None:
    """Переход prev → zone: если это портал Дорог Авалона — запомнить связь."""
    if not zone or not prev or zone == prev:
        return None
    if not (is_road(gd, zone) or is_road(gd, prev)):
        return None
    if zone in static_neighbors(gd, prev) or prev in static_neighbors(gd, zone):
        return None              # обычный переход карты, не портал
    return add_link(conn, prev, zone, source="auto", now=now)


def route(gd: GameData, link_rows: list[dict], start: str, goal: str, use_static: bool = True,
          now: int | None = None) -> dict | None:
    """Кратчайший по числу переходов путь; None — пути нет."""
    now = int(now or time.time())
    graph: dict[str, list[tuple[str, dict | None]]] = {}

    def edge(x, y, link):
        graph.setdefault(x, []).append((y, link))
        graph.setdefault(y, []).append((x, link))
    for l in link_rows:
        if l["expires"] is None or l["expires"] > now:
            edge(l["a"], l["b"], l)
    if use_static:
        for cid, (_name, _type, exits) in gd.clusters.items():
            for t in exits:
                edge(cid, t, None)
    if start == goal:
        return {"zones": [start], "steps": [], "portals": 0, "expires": None}
    prev: dict[str, tuple[str, dict | None]] = {start: ("", None)}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        if cur == goal:
            break
        for nxt, link in graph.get(cur, ()):
            if nxt not in prev:
                prev[nxt] = (cur, link)
                queue.append(nxt)
    if goal not in prev:
        return None
    steps, node = [], goal
    while node != start:
        parent, link = prev[node]
        steps.append({"from": parent, "to": node, "portal": link is not None,
                      "size": link["size"] if link else None, "expires": link["expires"] if link else None})
        node = parent
    steps.reverse()
    expiries = [s["expires"] for s in steps if s["expires"]]
    return {"zones": [start] + [s["to"] for s in steps], "steps": steps,
            "portals": sum(1 for s in steps if s["portal"]), "expires": min(expiries) if expiries else None}
