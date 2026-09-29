"""Калькуляторы производства: крафт и переработка, зачарование, дневники,
фермерство. Цены берутся из собранных заказов, рецепты — из gamedata."""

from __future__ import annotations

from dataclasses import dataclass

from .deals import SETUP_FEE
from .gamedata import GameData

FOCUS_BONUS = 0.59          # прибавка к бонусу производства при использовании фокуса
NUTRITION_PER_VALUE = 0.1125  # питание станции на единицу ценности предмета

CATEGORIES = [
    ("", "все"), ("weapons", "оружие"), ("head", "шлемы"), ("armors", "броня"), ("shoes", "обувь"),
    ("offhands", "вторая рука"), ("capes", "плащи"), ("bags", "сумки"), ("gathering", "снаряжение собирателя"),
    ("consumables", "еда и зелья"), ("mounts", "ездовые"), ("crafting", "ресурсы"), ("artefacts", "артефакты"),
    ("furniture", "мебель"), ("farming", "фермерство"), ("other", "прочее"),
]


def return_rate(bonus: float) -> float:
    """Доля возвращаемых ресурсов при бонусе производства (доля)."""
    return 1 - 1 / (1 + bonus) if bonus > 0 else 0.0


class PriceBook:
    """Лучшие цены по (предмет, рынок, качество) из списка заказов."""

    def __init__(self, orders: list[dict]):
        self.offer: dict[tuple, dict] = {}
        self.request: dict[tuple, dict] = {}
        for o in orders:
            key = (o["item_id"], o["location"], o["quality"])
            if o["auction_type"] == "offer":
                cur = self.offer.get(key)
                if cur is None or o["price"] < cur["price"]:
                    self.offer[key] = o
            else:
                cur = self.request.get(key)
                if cur is None or o["price"] > cur["price"]:
                    self.request[key] = o

    def buy_price(self, item: str, market: str, mode: str, quality: int = 1) -> float | None:
        """Сколько стоит купить: мгновенно — лучшее предложение, заказом — лучший заказ + комиссия."""
        if mode == "order":
            o = self.request.get((item, market, quality))
            return (o["price"] + 1) * (1 + SETUP_FEE) if o else None
        o = self.offer.get((item, market, quality))
        return float(o["price"]) if o else None

    def sell_price(self, item: str, market: str, mode: str, tax: float, quality: int = 1):
        """(цена в стакане, выручка после налога/комиссии) при продаже."""
        if mode == "order":
            o = self.offer.get((item, market, quality))
            return (o["price"], (o["price"] - 1) * (1 - tax - SETUP_FEE)) if o else (None, None)
        o = self.request.get((item, market, quality))
        return (o["price"], o["price"] * (1 - tax)) if o else (None, None)

    def seen(self, item: str, market: str, quality: int = 1):
        ts = [o["seen_at"] for o in (self.offer.get((item, market, quality)),
                                     self.request.get((item, market, quality))) if o]
        return min(ts) if ts else None


@dataclass
class CraftParams:
    kind: str = "craft"          # craft | refine
    buy_market: str = ""
    sell_market: str = ""
    craft_city: str = ""
    buy_mode: str = "instant"
    sell_mode: str = "instant"
    focus: bool = False
    tax: float = 0.04
    station_fee: float = 0.0     # серебро за 100 питания
    category: str = ""
    include_incomplete: bool = False


def craft_table(gd: GameData, book: PriceBook, p: CraftParams, item_ok=lambda i: True,
                volumes: dict | None = None) -> list[dict]:
    volumes = volumes or {}
    rows = []
    for out_id, rec in gd.recipes.items():
        if rec["kind"] != p.kind or not item_ok(out_id):
            continue
        meta = gd.items.get(out_id) or {}
        if p.category and meta.get("cat") != p.category:
            continue
        sell_book, revenue = book.sell_price(out_id, p.sell_market, p.sell_mode, p.tax)
        if revenue is None and not p.include_incomplete:
            continue
        bonus = gd.city_bonus(p.craft_city, out_id, p.kind == "refine") + (FOCUS_BONUS if p.focus else 0)
        rr = return_rate(bonus)
        materials, missing, parts = 0.0, [], []
        for res_id, count, returnable in rec["res"]:
            price = book.buy_price(res_id, p.buy_market, p.buy_mode)
            if price is None:
                missing.append(res_id)
                continue
            effective = count * (1 - rr) if returnable else count
            materials += price * effective
            parts.append({"item_id": res_id, "count": count, "price": round(price, 2), "returnable": returnable})
        if missing and not p.include_incomplete:
            continue
        fee = gd.item_value(out_id) * rec["n"] * NUTRITION_PER_VALUE * p.station_fee / 100
        cost = (materials + rec.get("silver", 0) + fee) / rec["n"]
        profit = revenue - cost if revenue is not None and not missing else None
        rows.append({
            "item_id": out_id, "tier": meta.get("t"), "category": meta.get("cat"),
            "amount": rec["n"], "return_rate": round(rr * 100, 2), "bonus": round(bonus * 100, 1),
            "materials": parts, "missing": missing,
            "station_fee": round(fee / rec["n"], 2), "cost": round(cost, 2),
            "sell_price": sell_book, "revenue": round(revenue, 2) if revenue is not None else None,
            "profit": round(profit, 2) if profit is not None else None,
            "margin": round(profit / cost * 100, 2) if profit is not None and cost else None,
            "daily_volume": volumes.get((out_id, p.sell_market, 1)),
            "focus": rec.get("focus", 0),
            "seen_at": book.seen(out_id, p.sell_market),
        })
    rows.sort(key=lambda r: (r["profit"] is not None, r["profit"] or 0), reverse=True)
    return rows


def enchant_table(gd: GameData, book: PriceBook, buy_market: str, sell_market: str, buy_mode: str,
                  sell_mode: str, tax: float, item_ok=lambda i: True, qualities=(1, 2, 3, 4, 5)) -> list[dict]:
    """Зачарование X.a → X.b рунами/душами/реликвиями против покупки готового X.b.
    Качество при зачаровании сохраняется."""
    rows = []
    for base, steps in gd.upgrades.items():
        by_level = {lvl: (mat, cnt) for lvl, mat, cnt in steps}
        levels = sorted(by_level)
        if not levels:
            continue
        top = max(levels)
        for q in qualities:
            for a in range(0, top):
                start_id = base if a == 0 else f"{base}@{a}"
                if not item_ok(start_id):
                    continue
                start_price = book.buy_price(start_id, buy_market, buy_mode, q)
                if start_price is None:
                    continue
                cost, used = start_price, []
                for b in range(a + 1, top + 1):
                    if b not in by_level:
                        break
                    mat, cnt = by_level[b]
                    mat_price = book.buy_price(mat, buy_market, buy_mode)
                    if mat_price is None:
                        break
                    cost += mat_price * cnt
                    used.append({"item_id": mat, "count": cnt, "price": round(mat_price, 2)})
                    target = f"{base}@{b}"
                    sell_book, revenue = book.sell_price(target, sell_market, sell_mode, tax, q)
                    if revenue is None:
                        continue
                    buy_ready = book.buy_price(target, buy_market, "instant", q)
                    profit = revenue - cost
                    rows.append({
                        "item_id": base, "from_id": start_id, "to_id": target, "from_level": a, "to_level": b,
                        "quality": q, "start_price": round(start_price, 2), "materials": list(used),
                        "cost": round(cost, 2), "sell_price": sell_book, "revenue": round(revenue, 2),
                        "profit": round(profit, 2), "margin": round(profit / cost * 100, 2) if cost else None,
                        "ready_price": buy_ready,
                        "saving": round(buy_ready - cost, 2) if buy_ready else None,
                        "seen_at": book.seen(target, sell_market, q),
                    })
    rows.sort(key=lambda r: r["profit"], reverse=True)
    return rows
