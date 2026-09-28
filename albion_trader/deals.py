"""Поиск выгодных сделок между рынками.

Модель торговли:

* Покупка в городе-источнике:
    - ``instant`` — выкупаем существующие предложения (sell orders), без комиссий;
    - ``order``   — выставляем заказ на покупку по цене лучшего запроса, платим
      комиссию за размещение (setup fee).
* Продажа в городе-назначении:
    - ``instant`` — продаём в существующие запросы (buy orders), платим налог с продажи;
    - ``order``   — выставляем предложение по цене лучшего предложения, платим
      налог и комиссию за размещение. На Чёрном рынке возможна только
      мгновенная продажа в запросы.

Качество: запрос на покупку качества *q* принимает предметы качества *q* и
выше, поэтому для мгновенной продажи подходят покупки любого качества ≥ *q*.
При продаже через собственное предложение качество должно совпадать.

Для мгновенной покупки и продажи стаканы «проходятся» от лучших цен, пока
сделка остаётся прибыльной, — так получается реальное количество и итоговая
прибыль, а не только разница верхних цен.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from .locations import BLACK_MARKET

TAX_PREMIUM = 0.04
TAX_NO_PREMIUM = 0.08
SETUP_FEE = 0.025


@dataclass
class DealParams:
    premium: bool = True
    buy_mode: str = "instant"   # instant | order
    sell_mode: str = "instant"  # instant | order
    sources: tuple = ()
    destinations: tuple = ()
    min_profit: float = 0          # прибыль с единицы, серебро
    min_margin: float = 0          # %, прибыль / вложения
    min_total_profit: float = 0
    tax: float | None = None       # переопределение налога, доля
    setup_fee: float = SETUP_FEE

    @property
    def sales_tax(self) -> float:
        if self.tax is not None:
            return self.tax
        return TAX_PREMIUM if self.premium else TAX_NO_PREMIUM


@dataclass
class Deal:
    item_id: str
    source: str
    destination: str
    buy_quality: int
    sell_quality: int
    buy_price: int          # цена в стакане источника, за шт.
    sell_price: int         # цена в стакане назначения, за шт. (до налогов)
    unit_cost: float        # с учётом комиссий
    unit_revenue: float     # после налогов и комиссий
    unit_profit: float
    margin: float           # %
    units: int | None       # None — количество не ограничено известными заказами
    total_profit: float
    investment: float
    buy_seen_at: int
    sell_seen_at: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Level:
    value: float            # стоимость покупки или чистая выручка за шт.
    amount: int | None      # None — без ограничения
    price: int
    quality: int
    seen_at: int


def _walk(buys: list[_Level], sells: list[_Level]):
    """Сопоставляет дешёвые покупки с дорогими продажами, пока есть прибыль."""
    buys = sorted(buys, key=lambda l: l.value)
    sells = sorted(sells, key=lambda l: -l.value)
    i = j = 0
    rb = buys[0].amount if buys else None
    rs = sells[0].amount if sells else None
    units = 0
    profit = invest = 0.0
    while i < len(buys) and j < len(sells) and sells[j].value > buys[i].value:
        if rb is None and rs is None:
            return None, sells[j].value - buys[i].value, buys[i].value
        take = rs if rb is None else rb if rs is None else min(rb, rs)
        units += take
        profit += take * (sells[j].value - buys[i].value)
        invest += take * buys[i].value
        if rb is not None:
            rb -= take
        if rs is not None:
            rs -= take
        if rb == 0:
            i += 1
            rb = buys[i].amount if i < len(buys) else None
        if rs == 0:
            j += 1
            rs = sells[j].amount if j < len(sells) else None
    return units, profit, invest


def _best(orders, pick):
    return pick(orders, key=lambda o: o["price"]) if orders else None


def find_deals(orders: list[dict], params: DealParams) -> list[Deal]:
    """``orders`` — записи с полями item_id, location, quality, price, amount,
    auction_type, seen_at (см. ``db.load_orders``)."""
    tax = params.sales_tax
    fee = params.setup_fee
    books: dict[tuple, dict[str, list]] = defaultdict(lambda: {"offer": [], "request": []})
    item_locations: dict[str, set] = defaultdict(set)
    for o in orders:
        books[(o["item_id"], o["location"])][o["auction_type"]].append(o)
        item_locations[o["item_id"]].add(o["location"])

    sources = set(params.sources) - {BLACK_MARKET}  # на Чёрном рынке нельзя покупать
    destinations = set(params.destinations)
    buy_side = "offer" if params.buy_mode == "instant" else "request"

    deals: list[Deal] = []
    for item_id, locs in item_locations.items():
        for src in locs & sources:
            src_book = books[(item_id, src)][buy_side]
            if not src_book:
                continue
            for dst in locs & destinations:
                if dst == src:
                    continue
                sell_mode = "instant" if dst == BLACK_MARKET else params.sell_mode
                dst_book = books[(item_id, dst)]["request" if sell_mode == "instant" else "offer"]
                if not dst_book:
                    continue
                for q in sorted({o["quality"] for o in dst_book}):
                    deal = _evaluate(item_id, src, dst, q, src_book, dst_book,
                                     params.buy_mode, sell_mode, tax, fee)
                    if deal and _passes(deal, params):
                        deals.append(deal)
    deals.sort(key=lambda d: (d.total_profit, d.unit_profit), reverse=True)
    return deals


def _evaluate(item_id, src, dst, q, src_book, dst_book, buy_mode, sell_mode, tax, fee):
    same_q = [o for o in dst_book if o["quality"] == q]
    if sell_mode == "instant":
        sells = [_Level(o["price"] * (1 - tax), o["amount"], o["price"], q, o["seen_at"])
                 for o in same_q]
    else:
        top = _best(same_q, min)
        sells = [_Level(top["price"] * (1 - tax - fee), None, top["price"], q, top["seen_at"])]

    if buy_mode == "instant":
        eligible = [o for o in src_book
                    if (o["quality"] >= q if sell_mode == "instant" else o["quality"] == q)]
        buys = [_Level(o["price"], o["amount"], o["price"], o["quality"], o["seen_at"])
                for o in eligible]
    else:
        top = _best([o for o in src_book if o["quality"] == q], max)
        buys = [] if top is None else [
            _Level(top["price"] * (1 + fee), None, top["price"], q, top["seen_at"])]
    if not buys or not sells:
        return None

    best_buy = min(buys, key=lambda l: l.value)
    best_sell = max(sells, key=lambda l: l.value)
    unit_profit = best_sell.value - best_buy.value
    if unit_profit <= 0:
        return None
    units, total, invest = _walk(buys, sells)
    if units is None:
        total, invest = unit_profit, best_buy.value
    return Deal(
        item_id=item_id, source=src, destination=dst,
        buy_quality=best_buy.quality, sell_quality=q,
        buy_price=best_buy.price, sell_price=best_sell.price,
        unit_cost=round(best_buy.value, 2), unit_revenue=round(best_sell.value, 2),
        unit_profit=round(unit_profit, 2),
        margin=round(unit_profit / best_buy.value * 100, 2),
        units=units, total_profit=round(total, 2), investment=round(invest, 2),
        buy_seen_at=best_buy.seen_at, sell_seen_at=best_sell.seen_at,
    )


def _passes(deal: Deal, p: DealParams) -> bool:
    return (deal.unit_profit >= p.min_profit
            and deal.margin >= p.min_margin
            and deal.total_profit >= p.min_total_profit)


def price_table(orders: list[dict]) -> list[dict]:
    """Сводка по одному предмету: лучшие цены на каждом рынке и в каждом качестве."""
    rows: dict[tuple, dict] = {}
    for o in orders:
        key = (o["location"], o["quality"])
        r = rows.setdefault(key, {
            "location": o["location"], "quality": o["quality"],
            "sell_min": None, "sell_amount": 0, "sell_seen_at": None,
            "buy_max": None, "buy_amount": 0, "buy_seen_at": None,
        })
        if o["auction_type"] == "offer":
            if r["sell_min"] is None or o["price"] < r["sell_min"]:
                r["sell_min"], r["sell_seen_at"] = o["price"], o["seen_at"]
            r["sell_amount"] += o["amount"]
        else:
            if r["buy_max"] is None or o["price"] > r["buy_max"]:
                r["buy_max"], r["buy_seen_at"] = o["price"], o["seen_at"]
            r["buy_amount"] += o["amount"]
    return sorted(rows.values(), key=lambda r: (r["location"], r["quality"]))
