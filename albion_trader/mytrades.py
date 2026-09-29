"""Учёт собственных заказов и сделок.

Источники (всё — пассивно из трафика, когда вы сами открываете окна игры):

* «Мои заказы» на рынке — открытые предложения и запросы;
* завершённые сделки (вкладка готовых к получению);
* мгновенные покупки (``AuctionBuyOffer``) и продажи в чужие заказы
  (``AuctionSellSpecificItemRequest``) — цена берётся из известного заказа;
* почта: итоги исполненных и истёкших заказов.

Формат почты у источников различается номерами параметров, поэтому массивы
списка писем ищутся по содержимому. Часть форматов подтверждена только
исходниками открытых инструментов — такие записи помечены «экспериментально».
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict
from typing import Callable

from .db import PRICE_SCALE, dotnet_ticks_to_unix, parse_expires
from .locations import normalize_location

log = logging.getLogger("albion_trader.mytrades")

SCHEMA = """
CREATE TABLE IF NOT EXISTS my_orders (
    id           INTEGER PRIMARY KEY,
    item_id      TEXT    NOT NULL,
    location     TEXT,
    quality      INTEGER NOT NULL,
    price        INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    auction_type TEXT    NOT NULL,
    expires      INTEGER,
    seen_at      INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS my_trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    kind       TEXT    NOT NULL,      -- buy | sell
    source     TEXT    NOT NULL,      -- instant | order | mail
    item_id    TEXT    NOT NULL,
    location   TEXT,
    quality    INTEGER,
    amount     INTEGER NOT NULL,
    unit_price REAL    NOT NULL,
    total      REAL    NOT NULL,
    key        TEXT    UNIQUE,
    experimental INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mails (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER,
    location TEXT,
    type     TEXT,
    body     TEXT
);
"""

LIST_GRACE = 60

SELL_FINISHED = ("MARKETPLACE_SELLORDER_FINISHED_SUMMARY",)
BUY_FINISHED = ("MARKETPLACE_BUYORDER_FINISHED_SUMMARY",)


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_mail_infos(params: dict) -> list[dict]:
    """Список писем: ищем массивы по содержимому (номера параметров меняются между версиями)."""
    arrays = {k: v for k, v in params.items() if isinstance(v, list) and v}
    types_key = next((k for k, v in arrays.items()
                      if all(isinstance(x, str) for x in v) and any("_SUMMARY" in x or "MARKETPLACE" in x for x in v)),
                     None)
    if types_key is None:
        return []
    n = len(arrays[types_key])
    ids_key = 3 if isinstance(params.get(3), list) and len(params[3]) == n else next(
        (k for k, v in arrays.items() if k != types_key and len(v) == n and all(isinstance(x, int) for x in v)
         and all(x < 10**12 for x in v)), None)
    ts_key = next((k for k, v in arrays.items() if len(v) == n and all(isinstance(x, int) for x in v)
                   and all(x > 10**15 for x in v)), None)
    loc_key = next((k for k, v in arrays.items() if k != types_key and len(v) == n
                    and all(isinstance(x, str) for x in v) and any(x[:4].isdigit() for x in v if x)), None)
    if ids_key is None:
        return []
    out = []
    for i in range(n):
        ts = arrays[ts_key][i] if ts_key is not None else None
        out.append({
            "id": int(arrays[ids_key][i]),
            "type": arrays[types_key][i],
            "location": arrays[loc_key][i] if loc_key is not None else None,
            "ts": dotnet_ticks_to_unix(ts) if ts else None,
        })
    return out


def parse_summary(body: str) -> dict | None:
    """Итог исполненного заказа: «количество|предмет|сумма|цена за штуку» (цены × 10 000)."""
    parts = (body or "").split("|")
    if len(parts) < 4:
        return None
    amount, total, unit = _int(parts[0]), _int(parts[2]), _int(parts[3])
    if not amount or total is None or not parts[1]:
        return None
    unit_price = unit / PRICE_SCALE if unit else total / PRICE_SCALE / amount
    return {"amount": amount, "item_id": parts[1], "total": total / PRICE_SCALE, "unit_price": unit_price}


class MyTrades:
    def __init__(self, conn_factory: Callable, write_lock, clock: Callable[[], float] = time.time,
                 on_orders: Callable[[], None] | None = None):
        self.conn_factory = conn_factory
        self.write_lock = write_lock
        self.clock = clock
        self.on_orders = on_orders
        self.mail_meta: dict[int, dict] = {}
        self.stats = {"orders_updates": 0, "trades": 0, "mails": 0}

    # --- подписка на сборщик -------------------------------------------
    def attach(self, state) -> None:
        state.on("my_orders", self.handle_my_orders)
        state.on("request:auction_buy_offer", self.handle_buy_offer)
        state.on("request:auction_sell_specific_item", self.handle_sell_to_request)
        state.on("response:get_mail_infos", self.handle_mail_infos)
        state.on("response:read_mail", self.handle_read_mail)

    def _write(self, fn):
        with self.write_lock, self.conn_factory() as conn:
            return fn(conn)

    def add_trade(self, conn, kind: str, source: str, item_id: str, amount: int, unit_price: float,
                  key: str, location=None, quality=None, ts=None, experimental=False) -> bool:
        cur = conn.execute(
            """INSERT OR IGNORE INTO my_trades(ts, kind, source, item_id, location, quality, amount, unit_price,
                                               total, key, experimental)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(ts or self.clock()), kind, source, item_id, location, quality, int(amount), float(unit_price),
             float(unit_price) * int(amount), key, int(experimental)))
        if cur.rowcount:
            self.stats["trades"] += 1
            log.info("Сделка: %s %s × %s по %s", "покупка" if kind == "buy" else "продажа", item_id, amount,
                     round(unit_price))
        return bool(cur.rowcount)

    # --- мои заказы ------------------------------------------------------
    def handle_my_orders(self, kind: str, orders: list[dict]) -> None:
        now = int(self.clock())

        def work(conn):
            if kind == "finished":
                for o in orders:
                    auction = (o.get("AuctionType") or "").lower()
                    price = _int(o.get("UnitPriceSilver"), 0) / PRICE_SCALE
                    amount = _int(o.get("Amount"), 0)
                    if not o.get("ItemTypeId") or not amount:
                        continue
                    self.add_trade(conn, "sell" if auction == "offer" else "buy", "order", o["ItemTypeId"], amount,
                                   price, f"finished:{o.get('Id')}", normalize_location(o.get("LocationId")),
                                   _int(o.get("QualityLevel"), 1), experimental=True)
                return
            seen_types = set()
            for o in orders:
                if not o.get("ItemTypeId") or o.get("Id") is None:
                    continue
                auction = (o.get("AuctionType") or ("offer" if kind == "offers" else "request")).lower()
                seen_types.add(auction)
                conn.execute(
                    """INSERT OR REPLACE INTO my_orders(id, item_id, location, quality, price, amount, auction_type,
                                                        expires, seen_at, active)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (int(o["Id"]), o["ItemTypeId"], normalize_location(o.get("LocationId")),
                     _int(o.get("QualityLevel"), 1), round(_int(o.get("UnitPriceSilver"), 0) / PRICE_SCALE),
                     _int(o.get("Amount"), 0), auction, parse_expires(o.get("Expires")), now))
            # Список «мои предложения/запросы» полный: заказов, которых в нём нет, больше не существует.
            types = {"offers": {"offer"}, "requests": {"request"}}.get(kind, seen_types)
            # Список может прийти несколькими страницами подряд — закрытыми считаем только
            # заказы, которых не было ни в одном ответе последние LIST_GRACE секунд.
            for t in types:
                conn.execute("UPDATE my_orders SET active = 0 WHERE auction_type = ? AND seen_at < ?",
                             (t, now - LIST_GRACE))
            self.stats["orders_updates"] += 1

        self._write(work)
        if self.on_orders and kind != "finished":
            self.on_orders()

    # --- мгновенные сделки ----------------------------------------------
    def _order_info(self, conn, auction_id):
        row = conn.execute("SELECT item_id, location, quality, price FROM orders WHERE id = ?",
                           (auction_id,)).fetchone()
        return dict(row) if row else None

    def handle_buy_offer(self, params: dict) -> None:
        auction_id, amount = _int(params.get(2)), _int(params.get(1), 1)
        if auction_id is None:
            return

        def work(conn):
            info = self._order_info(conn, auction_id)
            if info:
                self.add_trade(conn, "buy", "instant", info["item_id"], amount or 1, info["price"],
                               f"buy:{auction_id}:{int(self.clock())}", info["location"], info["quality"],
                               experimental=True)
        self._write(work)

    def handle_sell_to_request(self, params: dict) -> None:
        auction_id, amount = _int(params.get(1)), _int(params.get(4), 1)
        if auction_id is None:
            return

        def work(conn):
            info = self._order_info(conn, auction_id)
            if info:
                self.add_trade(conn, "sell", "instant", info["item_id"], amount or 1, info["price"],
                               f"sell:{auction_id}:{int(self.clock())}", info["location"], info["quality"],
                               experimental=True)
        self._write(work)

    # --- почта -----------------------------------------------------------
    def handle_mail_infos(self, params: dict) -> None:
        mails = parse_mail_infos(params)
        if not mails:
            return
        for m in mails:
            self.mail_meta[m["id"]] = m

        def work(conn):
            for m in mails:
                conn.execute("""INSERT INTO mails(id, ts, location, type) VALUES (?, ?, ?, ?)
                                ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, location=excluded.location,
                                                              type=excluded.type""",
                             (m["id"], m["ts"], m["location"], m["type"]))
        self._write(work)

    def handle_read_mail(self, params: dict) -> None:
        mail_id, body = _int(params.get(0)), params.get(1)
        if mail_id is None or not isinstance(body, str):
            return

        def work(conn):
            conn.execute("""INSERT INTO mails(id, body) VALUES (?, ?)
                            ON CONFLICT(id) DO UPDATE SET body=excluded.body""", (mail_id, body))
            self.stats["mails"] += 1
            meta = self.mail_meta.get(mail_id) or dict(
                conn.execute("SELECT id, ts, location, type FROM mails WHERE id = ?", (mail_id,)).fetchone() or {})
            mtype = meta.get("type") or ""
            if mtype not in SELL_FINISHED + BUY_FINISHED:
                return
            s = parse_summary(body)
            if not s:
                return
            self.add_trade(conn, "sell" if mtype in SELL_FINISHED else "buy", "mail", s["item_id"], s["amount"],
                           s["unit_price"], f"mail:{mail_id}", normalize_location(meta.get("location")),
                           None, ts=meta.get("ts"))
        self._write(work)


# --- отчёты ---------------------------------------------------------------

def open_orders(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM my_orders WHERE active = 1 ORDER BY seen_at DESC")]


def trades(conn, since: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM my_trades WHERE ts >= ? ORDER BY ts DESC", (since,))]


def summary(rows: list[dict], tax: float) -> dict:
    """Прибыль по предметам (средняя себестоимость покупок) и итоги по дням.

    Выручка продаж считается после налога (комиссии за размещение не видны в трафике).
    """
    per_item: dict[str, dict] = defaultdict(lambda: {"bought": 0, "cost": 0.0, "sold": 0, "revenue": 0.0})
    per_day: dict[str, dict] = defaultdict(lambda: {"bought": 0.0, "sold": 0.0, "profit": 0.0})
    for r in sorted(rows, key=lambda x: x["ts"]):
        it = per_item[r["item_id"]]
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        if r["kind"] == "buy":
            it["bought"] += r["amount"]
            it["cost"] += r["total"]
            per_day[day]["bought"] += r["total"]
        else:
            net = r["total"] * (1 - tax)
            avg_cost = it["cost"] / it["bought"] if it["bought"] else None
            it["sold"] += r["amount"]
            it["revenue"] += net
            per_day[day]["sold"] += net
            if avg_cost is not None:
                per_day[day]["profit"] += net - avg_cost * r["amount"]
    items = []
    for item_id, it in per_item.items():
        avg = it["cost"] / it["bought"] if it["bought"] else None
        profit = it["revenue"] - avg * it["sold"] if avg is not None and it["sold"] else None
        items.append({"item_id": item_id, "bought": it["bought"], "cost": round(it["cost"], 2),
                      "avg_cost": round(avg, 2) if avg is not None else None, "sold": it["sold"],
                      "revenue": round(it["revenue"], 2), "profit": round(profit, 2) if profit is not None else None,
                      # Остаток известен, только если всё проданное куплено через рынок.
                      "stock": it["bought"] - it["sold"] if it["bought"] >= it["sold"] else None})
    days = [{"day": d, **{k: round(v, 2) for k, v in vals.items()}} for d, vals in sorted(per_day.items(), reverse=True)]
    return {"items": items, "days": days}


def outbid_status(my_orders: list[dict], market: list[dict]) -> dict[int, dict]:
    """Для каждого моего активного заказа: лучший чужой заказ того же стакана и рекомендуемая цена.

    Предложение перебито, если кто-то продаёт дешевле; запрос — если кто-то покупает дороже.
    При равной цене первым исполняется более ранний заказ — это отмечается как «наравне».
    """
    mine_ids = {o["id"] for o in my_orders}
    best: dict[tuple, dict] = {}
    for o in market:
        if o.get("id") in mine_ids:
            continue
        key = (o["item_id"], o["location"], o["quality"], o["auction_type"])
        cur = best.get(key)
        if cur is None or (o["price"] < cur["price"] if o["auction_type"] == "offer" else o["price"] > cur["price"]):
            best[key] = o
    out = {}
    for m in my_orders:
        other = best.get((m["item_id"], m["location"], m["quality"], m["auction_type"]))
        if not other:
            out[m["id"]] = {"outbid": False, "tie": False, "best_price": None, "suggested_price": None}
            continue
        if m["auction_type"] == "offer":
            outbid, suggested = other["price"] < m["price"], other["price"] - 1
        else:
            outbid, suggested = other["price"] > m["price"], other["price"] + 1
        out[m["id"]] = {"outbid": outbid, "tie": other["price"] == m["price"], "best_price": other["price"],
                        "suggested_price": suggested if outbid or other["price"] == m["price"] else None,
                        "best_seen_at": other.get("seen_at")}
    return out
