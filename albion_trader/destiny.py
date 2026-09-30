"""Доска судьбы: названия узлов, слава по уровням, прогресс и прогноз."""

from __future__ import annotations

import json
import re
import time

from .gamedata import GameData

CATEGORY_NAMES = {"fighting": "Бой", "crafting": "Ремесло", "gathering": "Сбор", "farming": "Фермерство",
                  "tracking": "Выслеживание"}
_TIER_SUFFIX = re.compile(r"\s*\((?:[^()]*)\)\s*$")
_TIER_PREFIX_EN = re.compile(r"^(?:Beginner|Novice|Journeyman|Adept|Expert|Master|Grandmaster|Elder)'s\s+")


def strip_tier(name: str) -> str:
    """«Морозный посох (старейшина)» → «Морозный посох»; «Elder's Frost Staff» → «Frost Staff»."""
    return _TIER_PREFIX_EN.sub("", _TIER_SUFFIX.sub("", name or ""))


GATHER_RESOURCES = {"FIBER": "волокно", "HIDE": "шкуры", "ORE": "руда", "ROCK": "камень", "WOOD": "древесина"}
MISSION_WORDS = {"killmobfame": "бой мобы", "craftitemfame": "ремесло крафт", "gatherfame": "сбор собирательство",
                 "fishingfame": "рыбалка рыба", "farmharvestfame": "ферма урожай", "farmraisefame": "ферма животные",
                 "trackingfame": "выслеживание"}
_TPL_TIER = re.compile(r"_T(\d)$")


def node_title(gd: GameData, name_of, node_id: str) -> str:
    """Название узла: предмет-иконка без пометки тира; у общих веток — «(ветка)».

    Узлы сбора и переработки одинаковы по иконке для всех тиров — к ним
    добавляется ресурс и тир из шаблона (``GATHER_T5`` → «T5»).
    """
    node = gd.destiny.get("nodes", {}).get(node_id)
    if not node:
        return node_id
    item = node.get("item") or ""
    name = name_of(item) if item else ""
    if not name or name == item:
        name = node_id.split("_", 1)[-1].replace("_", " ").title()
    name = strip_tier(name)
    if node.get("mission") == "fishingfame":
        name = "Рыбалка" + (": улов" if node_id.endswith("_FISH_FISH") else "")
    elif node.get("mission") == "gatherfame":
        res = next((ru for key, ru in GATHER_RESOURCES.items() if f"_{key}" in node_id), "")
        name = f"{res.capitalize()} ({name})" if res else name
    elif node.get("tpl", "").startswith("REFINE_"):
        name = f"Переработка ({name})"
    tier = _TPL_TIER.search(node.get("tpl", ""))
    if tier:
        name += f" T{tier.group(1)}"
    if node.get("base"):
        name += " (ветка)"
    return f"{CATEGORY_NAMES.get(node.get('cat'), node.get('cat') or '')}: {name}".strip(": ")


# --- прогресс ------------------------------------------------------------------

MAX_LEVEL = 100
# Какая слава идёт в узел: категория доски → источник славы из activity.
CATEGORY_SOURCE = {"fighting": "combat", "gathering": "gathering", "crafting": "crafting"}
IDLE_GAP = 10 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS destiny_track (
    node_id  TEXT PRIMARY KEY,
    level    INTEGER NOT NULL,
    progress REAL    NOT NULL,
    target   INTEGER NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1,
    since    INTEGER NOT NULL
);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)


def node_source(node: dict) -> str | None:
    """Источник славы узла или None (фермерство, выслеживание — не отслеживаются автоматически)."""
    if node.get("mission") == "fishingfame":
        return "fishing"
    return CATEGORY_SOURCE.get(node.get("cat"))


def level_table(gd: GameData, node_id: str) -> list[float]:
    """Слава для перехода с уровня i на i+1 (i = 0…99) с учётом множителя узла."""
    node = gd.destiny.get("nodes", {}).get(node_id)
    if not node:
        return []
    table = gd.destiny.get("templates", {}).get(node["tpl"], [])
    return [f * node.get("mult", 1.0) for f in table[:MAX_LEVEL]]


def remaining(table: list[float], level: int, progress: float, target: int) -> float:
    target = min(target, len(table))
    if level >= target:
        return 0.0
    return max(sum(table[level:target]) - progress, 0.0)


def advance(table: list[float], level: int, progress: float, gained: float) -> tuple[int, float]:
    """Прибавить славу: уровень растёт, пока хватает славы на следующий."""
    progress += max(gained, 0.0)
    while level < len(table) and progress >= table[level]:
        progress -= table[level]
        level += 1
    if level >= len(table):
        progress = 0.0
    return level, progress


def fame_by_source(conn, since: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in conn.execute("SELECT value, data FROM activity_events WHERE kind = 'fame' AND ts >= ?", (since,)):
        try:
            src = json.loads(r["data"] or "{}").get("src") or "combat"
        except ValueError:
            src = "combat"
        out[src] = out.get(src, 0.0) + (r["value"] or 0)
    return out


def played_seconds(conn, since: int) -> int:
    """Время в игре: промежутки между событиями, не длиннее IDLE_GAP (отошли — не считаем)."""
    total, prev = 0, None
    for r in conn.execute("SELECT session_id, ts FROM activity_events WHERE ts >= ? ORDER BY session_id, ts",
                          (since,)):
        if prev and prev[0] == r["session_id"]:
            total += min(max(r["ts"] - prev[1], 0), IDLE_GAP)
        prev = (r["session_id"], r["ts"])
    return total


def track(conn, gd: GameData, node_id: str, level: int, progress: float, target: int, active: bool,
          now: int | None = None) -> None:
    table = level_table(gd, node_id)
    if not table:
        raise ValueError("неизвестный узел Доски судьбы")
    level = min(max(int(level), 0), len(table))
    target = min(max(int(target), level + 1 if level < len(table) else level), len(table))
    progress = max(float(progress), 0.0)
    if level < len(table):
        progress = min(progress, table[level])
    conn.execute("INSERT OR REPLACE INTO destiny_track(node_id, level, progress, target, active, since) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (node_id, level, progress, target, int(bool(active)),
                                               int(now or time.time())))


def untrack(conn, node_id: str) -> None:
    conn.execute("DELETE FROM destiny_track WHERE node_id = ?", (node_id,))


def report(conn, gd: GameData, name_of, now: int, days: float = 7) -> dict:
    period_start = now - int(days * 86400)
    hours = played_seconds(conn, period_start) / 3600
    by_src = fame_by_source(conn, period_start)
    rates = {src: (by_src.get(src, 0.0) / hours if hours >= 0.05 else None) for src in
             ("combat", "gathering", "crafting", "fishing")}
    rows = []
    for t in conn.execute("SELECT * FROM destiny_track ORDER BY since"):
        t = dict(t)
        node = gd.destiny.get("nodes", {}).get(t["node_id"])
        table = level_table(gd, t["node_id"])
        if not node or not table:
            continue
        src = node_source(node)
        gained = fame_by_source(conn, t["since"]).get(src, 0.0) if t["active"] and src else 0.0
        level, progress = advance(table, t["level"], t["progress"], gained)
        left = remaining(table, level, progress, t["target"])
        rate = rates.get(src) if src else None
        next_need = table[level] if level < len(table) else None
        rows.append({
            "node_id": t["node_id"], "title": node_title(gd, name_of, t["node_id"]), "source": src,
            "active": bool(t["active"]), "since": t["since"], "start_level": t["level"],
            "start_progress": round(t["progress"]), "gained": round(gained),
            "level": level, "progress": round(progress), "next_level_fame": round(next_need) if next_need else None,
            "target": t["target"], "remaining": round(left),
            "to_target_total": round(sum(table[t["level"]:t["target"]])),
            "rate": round(rate) if rate else None,
            "eta_hours": round(left / rate, 1) if rate and left else (0 if not left else None),
        })
    return {"rows": rows, "rates": {k: (round(v) if v else None) for k, v in rates.items()},
            "hours": round(hours, 2), "days": days}


def search_nodes(gd: GameData, name_of, query: str, limit: int = 40) -> list[dict]:
    words = (query or "").lower().split()
    out = []
    for nid, node in gd.destiny.get("nodes", {}).items():
        title = node_title(gd, name_of, nid)
        text = f"{title} {nid} {MISSION_WORDS.get(node.get('mission'), '')}".lower()
        if words and not all(w in text for w in words):
            continue
        table = level_table(gd, nid)
        out.append({"node_id": nid, "title": title, "source": node_source(node), "base": node.get("base", False),
                    "total_fame": round(sum(table))})
    out.sort(key=lambda n: n["title"])
    return out[:limit]
