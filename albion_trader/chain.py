"""Цепочка производства целиком: предмет → ресурсы → сырьё.

Для каждого узла дерева считаются два варианта — купить на рынке (самый дешёвый
из выбранных рынков) и сделать самому (крафт или переработка с возвратом
ресурсов по бонусу города и фокусу) — и выбирается более дешёвый. Итог —
минимальная себестоимость верхнего предмета и прибыль при продаже.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .gamedata import GameData
from .production import FOCUS_BONUS, NUTRITION_PER_VALUE, PriceBook, return_rate

MAX_DEPTH = 8


@dataclass
class ChainParams:
    buy_markets: list = field(default_factory=list)   # где можно покупать (берётся самый дешёвый)
    buy_mode: str = "instant"
    craft_city: str = ""        # где крафтить (бонус города)
    refine_city: str = ""       # где перерабатывать
    focus: bool = False
    sell_market: str = ""
    sell_mode: str = "instant"
    quality: int = 1            # качество при продаже верхнего предмета
    tax: float = 0.04
    station_fee: float = 0.0    # серебро за 100 питания
    max_depth: int = MAX_DEPTH


def items_in_chain(gd: GameData, item_id: str, max_depth: int = MAX_DEPTH) -> set[str]:
    """Все предметы дерева — чтобы загрузить цены только по ним."""
    seen: set[str] = set()
    stack = [(item_id, 0)]
    while stack:
        item, depth = stack.pop()
        if item in seen:
            continue
        seen.add(item)
        rec = gd.recipes.get(item)
        if rec and rec.get("kind") in ("craft", "refine") and depth < max_depth:
            stack.extend((r[0], depth + 1) for r in rec["res"])
    return seen


def _cheapest(book: PriceBook, item: str, markets: list, mode: str) -> tuple[float | None, str | None]:
    best, where = None, None
    for m in markets:
        price = book.buy_price(item, m, mode)
        if price is not None and (best is None or price < best):
            best, where = price, m
    return best, where


def _node(gd: GameData, book: PriceBook, item: str, qty: float, p: ChainParams, depth: int,
          path: frozenset, top: bool = False) -> dict:
    buy_unit, buy_market = _cheapest(book, item, p.buy_markets, p.buy_mode)
    node = {"item_id": item, "qty": qty, "depth": depth, "buy_unit": buy_unit, "buy_market": buy_market,
            "buy_total": buy_unit * qty if buy_unit is not None else None,
            "craft_total": None, "craft_unit": None, "children": [], "kind": None}
    rec = gd.recipes.get(item)
    can_craft = (rec and rec.get("kind") in ("craft", "refine") and depth < p.max_depth
                 and item not in path and rec["res"])
    if can_craft:
        refine = rec["kind"] == "refine"
        city = p.refine_city if refine else p.craft_city
        bonus = gd.city_bonus(city, item, refine) + (FOCUS_BONUS if p.focus else 0)
        rr = return_rate(bonus)
        crafts = qty / (rec.get("n") or 1)
        total, complete = 0.0, True
        for res_id, count, returnable in rec["res"]:
            need = count * crafts * ((1 - rr) if returnable else 1)
            child = _node(gd, book, res_id, need, p, depth + 1, path | {item})
            node["children"].append(child)
            if child["best_total"] is None:
                complete = False
            else:
                total += child["best_total"]
        fee = gd.item_value(item) * (rec.get("n") or 1) * crafts * NUTRITION_PER_VALUE * p.station_fee / 100
        silver = rec.get("silver", 0) * crafts
        node.update(kind=rec["kind"], bonus=round(bonus * 100, 1), return_rate=round(rr * 100, 2),
                    crafts=crafts, station_fee=fee, silver=silver,
                    focus=rec.get("focus", 0) * crafts if p.focus else 0)
        if complete:
            node["craft_total"] = total + fee + silver
            node["craft_unit"] = node["craft_total"] / qty if qty else None
    buy, craft = node["buy_total"], node["craft_total"]
    if top and craft is not None:
        decision = "craft"           # верхний предмет делаем — иначе это просто перепродажа
    elif buy is None and craft is None:
        decision = "missing"
    elif craft is None or (buy is not None and buy <= craft):
        decision = "buy"
    else:
        decision = "craft"
    node["decision"] = decision
    node["best_total"] = craft if decision == "craft" else buy if decision == "buy" else None
    node["saving"] = (buy - craft) if buy is not None and craft is not None else None
    return node


def _walk(node: dict, chosen_only: bool = True):
    yield node
    if not chosen_only or node["decision"] == "craft":
        for c in node["children"]:
            yield from _walk(c, chosen_only)


def _missing(node: dict) -> set[str]:
    """Что именно мешает расчёту: листья без цены (а не их родители)."""
    if node["decision"] == "buy":
        return set()
    found = set().union(*(_missing(c) for c in node["children"])) if node["children"] else set()
    if node["decision"] == "missing" and not found:
        found = {node["item_id"]}
    return found


def build_chain(gd: GameData, book: PriceBook, item_id: str, qty: float, p: ChainParams) -> dict:
    qty = max(float(qty), 1.0)
    tree = _node(gd, book, item_id, qty, p, 0, frozenset(), top=True)
    sell_book, revenue_unit = book.sell_price(item_id, p.sell_market, p.sell_mode, p.tax, p.quality)
    cost = tree["best_total"] if tree["decision"] == "craft" else tree["craft_total"]
    revenue = revenue_unit * qty if revenue_unit is not None else None
    profit = revenue - cost if revenue is not None and cost is not None else None
    chosen = list(_walk(tree))
    shopping: dict[tuple, dict] = {}
    for n in chosen:
        if n["decision"] == "buy" and n is not tree:
            key = (n["item_id"], n["buy_market"])
            s = shopping.setdefault(key, {"item_id": n["item_id"], "market": n["buy_market"], "qty": 0.0,
                                          "unit": n["buy_unit"], "total": 0.0})
            s["qty"] += n["qty"]
            s["total"] += n["buy_total"]
    for s in shopping.values():
        s["qty"], s["total"] = round(s["qty"], 2), round(s["total"], 2)
    missing = sorted(_missing(tree))
    return {
        "item_id": item_id, "qty": qty, "tree": _round(tree),
        "cost": cost, "cost_unit": cost / qty if cost is not None else None,
        "sell_price": sell_book, "revenue": revenue, "profit": profit,
        "margin": profit / cost * 100 if profit is not None and cost else None,
        "focus": sum(n.get("focus", 0) for n in chosen if n["decision"] == "craft"),
        "shopping": sorted(shopping.values(), key=lambda s: -s["total"]),
        "missing": missing, "has_recipe": tree["kind"] is not None,
    }


def _round(node: dict) -> dict:
    for k in ("qty", "buy_unit", "buy_total", "craft_total", "craft_unit", "best_total", "saving",
              "crafts", "station_fee", "silver", "focus"):
        if isinstance(node.get(k), float):
            node[k] = round(node[k], 2)
    for c in node["children"]:
        _round(c)
    return node
