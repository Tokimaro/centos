"""Конструктор билдов: цена комплекта по городам и сила предметов.

Сила предмета (IP) — база из справочника (у зачарованных вариантов своя) плюс
прибавка за качество. Средняя сила считается по шести слотам (оружие, вторая
рука, голова, броня, обувь, плащ); двуручное оружие занимает и вторую руку.
Мастерство и специализация персонажа не учитываются.
"""

from __future__ import annotations

import json
import time

from .gamedata import GameData

SLOTS = [("mainhand", "Оружие"), ("offhand", "Вторая рука"), ("head", "Голова"), ("armor", "Броня"),
         ("shoes", "Обувь"), ("cape", "Плащ"), ("bag", "Сумка"), ("mount", "Ездовое"), ("food", "Еда"),
         ("potion", "Зелье")]
SLOT_KEYS = [s for s, _ in SLOTS]
IP_SLOTS = ("mainhand", "offhand", "head", "armor", "shoes", "cape")
QUALITY_IP = {1: 0, 2: 20, 3: 40, 4: 60, 5: 100}
STACKABLE = ("food", "potion")

SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL,
    data    TEXT    NOT NULL,
    updated INTEGER NOT NULL
);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)


def clean_build(raw, gd: GameData | None = None) -> dict:
    """Проверяет билд: {"name", "slots": {слот: {"item", "quality", "count"}}}."""
    if not isinstance(raw, dict):
        raise ValueError("билд должен быть объектом")
    slots_raw = raw.get("slots")
    if slots_raw is None:
        slots_raw = {}
    if not isinstance(slots_raw, dict):
        raise ValueError("слоты билда должны быть объектом")
    slots = {}
    for slot, v in slots_raw.items():
        if slot not in SLOT_KEYS:
            raise ValueError(f"неизвестный слот: {slot}")
        if not isinstance(v, dict):
            raise ValueError(f"слот {slot}: ожидается объект")
        item = v.get("item")
        if item in (None, ""):
            continue
        if not isinstance(item, str) or len(item) > 80 or not item.replace("@", "").replace("_", "").isalnum():
            raise ValueError(f"слот {slot}: некорректный предмет")
        meta = (gd.items.get(item) if gd is not None else None) or {}
        if meta.get("slot") and meta["slot"] != slot:
            raise ValueError(f"{item} не подходит в слот «{dict(SLOTS)[slot]}»")
        try:
            quality = int(v.get("quality") or 1)
            count = int(v.get("count") or 1)
        except (TypeError, ValueError):
            raise ValueError(f"слот {slot}: качество и количество — числа") from None
        slots[slot] = {"item": item, "quality": min(max(quality, 1), 5),
                       "count": min(max(count, 1), 999) if slot in STACKABLE else 1}
    main = slots.get("mainhand")
    if main and gd is not None and (gd.items.get(main["item"]) or {}).get("2h") and "offhand" in slots:
        raise ValueError("двуручное оружие занимает и вторую руку")
    name = str(raw.get("name") or "").strip()[:80] or "Без названия"
    return {"name": name, "slots": slots}


def item_power(gd: GameData, item: str, quality: int) -> float | None:
    base = (gd.items.get(item) or {}).get("ip")
    return base + QUALITY_IP.get(quality, 0) if base is not None else None


def average_ip(gd: GameData, slots: dict) -> float | None:
    values = []
    main = slots.get("mainhand")
    two_handed = bool(main and (gd.items.get(main["item"]) or {}).get("2h"))
    for slot in IP_SLOTS:
        s = slots.get(slot)
        if slot == "offhand" and two_handed:
            s = main
        ip = item_power(gd, s["item"], s["quality"]) if s else None
        values.append(ip or 0)
    return sum(values) / len(IP_SLOTS) if any(values) else None


def best_offers(orders: list[dict]) -> dict:
    """{(предмет, рынок): [(качество, цена), ...]} по предложениям."""
    out: dict[tuple, list] = {}
    for o in orders:
        if o["auction_type"] == "offer":
            out.setdefault((o["item_id"], o["location"]), []).append((o["quality"], o["price"], o["seen_at"]))
    return out


def price_build(gd: GameData, build: dict, orders: list[dict], markets: list[str]) -> dict:
    offers = best_offers(orders)
    rows = []
    totals = {m: {"market": m, "total": 0.0, "missing": []} for m in markets}
    cheapest_total, complete = 0.0, True
    for slot, title in SLOTS:
        s = build["slots"].get(slot)
        if not s:
            continue
        per_market = {}
        for m in markets:
            cands = [(price, seen) for q, price, seen in offers.get((s["item"], m), []) if q >= s["quality"]]
            if cands:
                price, seen = min(cands)
                per_market[m] = {"price": price * s["count"], "unit": price, "seen_at": seen}
        best_m = min(per_market, key=lambda m: per_market[m]["price"]) if per_market else None
        for m in markets:
            if m in per_market:
                totals[m]["total"] += per_market[m]["price"]
            else:
                totals[m]["missing"].append(slot)
        if best_m:
            cheapest_total += per_market[best_m]["price"]
        else:
            complete = False
        meta = gd.items.get(s["item"]) or {}
        rows.append({"slot": slot, "slot_name": title, "item_id": s["item"], "quality": s["quality"],
                     "count": s["count"], "tier": meta.get("t"), "ip": item_power(gd, s["item"], s["quality"]),
                     "spec": meta.get("spec"), "best_market": best_m,
                     "best_price": per_market[best_m]["price"] if best_m else None, "markets": per_market})
    market_rows = sorted(totals.values(), key=lambda t: (len(t["missing"]), t["total"]))
    for t in market_rows:
        t["complete"] = not t["missing"]
    return {"rows": rows, "markets": market_rows, "cheapest_total": cheapest_total if rows else None,
            "complete": complete and bool(rows), "average_ip": average_ip(gd, build["slots"])}


# --- хранение --------------------------------------------------------------

def list_builds(conn) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "updated": r["updated"], **json.loads(r["data"])}
            for r in conn.execute("SELECT * FROM builds ORDER BY updated DESC")]


def save_build(conn, build: dict, build_id: int | None = None, now: int | None = None) -> int:
    now = int(now or time.time())
    data = json.dumps({"slots": build["slots"]}, ensure_ascii=False)
    if build_id:
        cur = conn.execute("UPDATE builds SET name = ?, data = ?, updated = ? WHERE id = ?",
                           (build["name"], data, now, build_id))
        if cur.rowcount:
            return build_id
    return conn.execute("INSERT INTO builds(name, data, updated) VALUES (?, ?, ?)",
                        (build["name"], data, now)).lastrowid


def delete_build(conn, build_id: int) -> None:
    conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
