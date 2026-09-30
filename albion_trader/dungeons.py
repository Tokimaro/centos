"""Журнал данжей и сундуков.

Прохождение — время от входа в зону-данж до выхода в обычную зону; соседние
уровни одного данжа (переходы данж → данж) считаются одним прохождением. Тип
зоны берётся из карты мира (``cluster/world.json``); зоны, которых там нет и
которые не похожи на город, Дорогу Авалона или остров, — это инстансы
(случайные данжи и т. п.).

По каждому прохождению: длительность, слава, серебро с мобов, лут (ваш, по
рыночной оценке), открытые сундуки по редкости, смерти.
"""

from __future__ import annotations

import json
from typing import Callable

from .gamedata import GameData

KINDS = {"static": "статичный данж", "corrupted": "коррапт", "hellgate": "хеллгейт",
         "expedition": "экспедиция", "mists": "туманы", "instance": "случайный данж / инстанс"}
RARITY = {0: "обычный", 1: "необычный", 2: "редкий", 3: "легендарный"}


def dungeon_kind(gd: GameData, zone: str | None) -> str | None:
    """Тип данжа или None, если это обычная зона."""
    if not zone:
        return None
    ctype = gd.cluster_type(zone)
    up = zone.upper()
    if ctype.startswith("CORRUPTED") or up.startswith("CORRUPT"):
        return "corrupted"
    if ctype.startswith("DUNGEON_HELL") or up.startswith("HELLGATE"):
        return "hellgate"
    if "EXPEDITION" in ctype:
        return "expedition"
    if "MIST" in ctype or "MIST" in up:
        return "mists"
    if ctype.startswith("DUNGEON") or up.startswith("DNG-"):
        return "static"
    if ctype:
        return None                     # известная зона карты, не данж
    if (zone.isdigit() or up.startswith(("TNL-", "@ISLAND@", "ISLAND-", "BLACKBANK-", "PSG-", "HIDEOUT", "ARENA"))
            or up.endswith(("-HELLDEN", "-AUCTION2"))):
        return None
    return "instance"


def runs(conn, gd: GameData, since: int, character: str, value_of: Callable[[str], float | None],
         now: int) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    last_ts, last_session = None, None

    def close(end_ts):
        nonlocal cur
        if cur:
            cur["ended"] = end_ts
            out.append(cur)
        cur = None

    for e in conn.execute("SELECT * FROM activity_events WHERE ts >= ? ORDER BY session_id, ts, id", (since,)):
        e = dict(e)
        if last_session is not None and e["session_id"] != last_session:
            close(last_ts)
        last_session, last_ts = e["session_id"], e["ts"]
        if e["kind"] == "zone":
            kind = dungeon_kind(gd, e["target"])
            if kind and cur is None:
                cur = {"started": e["ts"], "kind": kind, "zones": [e["target"]], "fame": 0.0, "silver": 0.0,
                       "loot_silver": 0.0, "items": {}, "chests": {}, "deaths": 0, "session_id": e["session_id"]}
            elif kind and cur is not None:
                if e["target"] not in cur["zones"]:
                    cur["zones"].append(e["target"])
            elif cur is not None:
                close(e["ts"])
            continue
        if cur is None:
            continue
        if e["kind"] == "fame":
            cur["fame"] += e["value"] or 0
        elif e["kind"] == "silver":
            cur["silver"] += e["value"] or 0
        elif e["kind"] == "loot" and (not character or e["actor"] == character):
            if json.loads(e["data"] or "{}").get("silver"):
                cur["loot_silver"] += e["amount"] or 0
            elif e["item_id"]:
                cur["items"][e["item_id"]] = cur["items"].get(e["item_id"], 0) + (e["amount"] or 0)
        elif e["kind"] == "chest":
            key = str(int(e["amount"])) if e["amount"] is not None else "?"
            cur["chests"][key] = cur["chests"].get(key, 0) + 1
        elif e["kind"] == "death" and character and e["target"] == character:
            cur["deaths"] += 1
    if cur:
        # Прохождение ещё идёт (или сессия оборвалась внутри данжа).
        cur["ended"] = None
        cur["last_event"] = last_ts
        out.append(cur)

    for r in out:
        end = r["ended"] or r.get("last_event") or now
        r["seconds"] = max(end - r["started"], 0)
        value, unpriced = 0.0, 0
        for item, amount in r.pop("items").items():
            price = value_of(item) if not item.startswith("#") else None
            if price is None:
                unpriced += 1
            else:
                value += price * amount
        r["loot_value"] = round(value)
        r["unpriced_items"] = unpriced
        r["income"] = round(r["silver"] + r["loot_silver"] + value)
        hours = r["seconds"] / 3600
        r["fame"], r["silver"], r["loot_silver"] = round(r["fame"]), round(r["silver"]), round(r["loot_silver"])
        r["fame_per_hour"] = round(r["fame"] / hours) if hours >= 1 / 60 else None
        r["income_per_hour"] = round(r["income"] / hours) if hours >= 1 / 60 else None
        r["chest_total"] = sum(r["chests"].values())
        r["active"] = r["ended"] is None
    out.sort(key=lambda r: r["started"], reverse=True)
    return out


def summary(run_rows: list[dict]) -> list[dict]:
    """Сводка по типам данжей: число, среднее время, слава и доход в час, сундуки."""
    by: dict[str, dict] = {}
    for r in run_rows:
        s = by.setdefault(r["kind"], {"kind": r["kind"], "runs": 0, "seconds": 0, "fame": 0, "income": 0,
                                      "chests": {}, "deaths": 0})
        s["runs"] += 1
        s["seconds"] += r["seconds"]
        s["fame"] += r["fame"]
        s["income"] += r["income"]
        s["deaths"] += r["deaths"]
        for k, n in r["chests"].items():
            s["chests"][k] = s["chests"].get(k, 0) + n
    out = []
    for s in by.values():
        hours = s["seconds"] / 3600
        s["avg_minutes"] = round(s["seconds"] / 60 / s["runs"], 1)
        s["fame_per_hour"] = round(s["fame"] / hours) if hours >= 1 / 60 else None
        s["income_per_hour"] = round(s["income"] / hours) if hours >= 1 / 60 else None
        s["name"] = KINDS.get(s["kind"], s["kind"])
        out.append(s)
    out.sort(key=lambda s: s["income_per_hour"] or 0, reverse=True)
    return out
