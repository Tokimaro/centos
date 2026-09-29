"""Список наблюдения и оповещения.

Правила хранятся в ``alert_rules``, сработавшие оповещения — в ``alerts``.
Рыночные правила проверяются после каждого пакета заказов для затронутых
предметов; остальные разделы (перебитые заказы, события мира) создают
оповещения через ``AlertEngine.trigger``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Callable

from . import db
from .deals import DealParams, find_deals, underpriced

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    params   TEXT    NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    created  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    rule_id  INTEGER,
    kind     TEXT    NOT NULL,
    title    TEXT    NOT NULL,
    text     TEXT    NOT NULL,
    key      TEXT    NOT NULL,
    payload  TEXT,
    seen     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, ts);
"""

KINDS = {
    "price_below": "Цена ниже",
    "price_above": "Цена выше",
    "deal": "Сделка между рынками",
    "underpriced": "Недооценённый лот",
    "outbid": "Мой заказ перебили",
    "world_event": "Событие мира",
}
MARKET_KINDS = ("price_below", "price_above", "deal", "underpriced")
REPEAT_SECONDS = 30 * 60
KEEP_DAYS = 30


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


class AlertEngine:
    def __init__(self, conn_factory: Callable, name_of: Callable[[str], str] = lambda i: i,
                 loc_name: Callable[[str], str] = lambda l: l, clock: Callable[[], float] = time.time):
        self.conn_factory = conn_factory
        self.name_of = name_of
        self.loc_name = loc_name
        self.clock = clock
        self.listeners: list[Callable[[dict], None]] = []
        self._touched: set | None = None

    # --- правила ------------------------------------------------------
    def rules(self, conn, kind: str | None = None, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT id, name, kind, params, enabled, created FROM alert_rules"
        cond, args = [], []
        if kind:
            cond.append("kind = ?")
            args.append(kind)
        if enabled_only:
            cond.append("enabled = 1")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        out = []
        for r in conn.execute(sql + " ORDER BY id", args):
            d = dict(r)
            d["params"] = json.loads(d["params"])
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def save_rule(self, conn, rule: dict) -> int:
        kind = rule.get("kind")
        if kind not in KINDS:
            raise ValueError(f"неизвестный тип правила: {kind}")
        params = rule.get("params") or {}
        name = (rule.get("name") or "").strip() or KINDS[kind]
        if rule.get("id"):
            conn.execute("UPDATE alert_rules SET name=?, kind=?, params=?, enabled=? WHERE id=?",
                         (name, kind, json.dumps(params, ensure_ascii=False), int(rule.get("enabled", True)),
                          int(rule["id"])))
            return int(rule["id"])
        cur = conn.execute(
            "INSERT INTO alert_rules(name, kind, params, enabled, created) VALUES (?, ?, ?, ?, ?)",
            (name, kind, json.dumps(params, ensure_ascii=False), int(rule.get("enabled", True)), int(self.clock())))
        return cur.lastrowid

    def ensure_rule(self, conn, kind: str, name: str, params: dict | None = None) -> None:
        """Создаёт правило по умолчанию, если правил этого типа ещё нет."""
        if not conn.execute("SELECT 1 FROM alert_rules WHERE kind = ?", (kind,)).fetchone():
            self.save_rule(conn, {"kind": kind, "name": name, "params": params or {}})

    # --- оповещения ---------------------------------------------------
    def trigger(self, conn, kind: str, key: str, title: str, text: str, payload: dict | None = None,
                rule_id: int | None = None) -> dict | None:
        """Создаёт оповещение, если такое же не срабатывало последние 30 минут."""
        now = int(self.clock())
        full_key = f"{rule_id or kind}:{key}"
        if conn.execute("SELECT 1 FROM alerts WHERE key = ? AND ts >= ?",
                        (full_key, now - REPEAT_SECONDS)).fetchone():
            return None
        cur = conn.execute(
            "INSERT INTO alerts(ts, rule_id, kind, title, text, key, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, rule_id, kind, title, text, full_key, json.dumps(payload or {}, ensure_ascii=False)))
        alert = {"id": cur.lastrowid, "ts": now, "rule_id": rule_id, "kind": kind, "title": title,
                 "text": text, "payload": payload or {}}
        for fn in self.listeners:
            try:
                fn(alert)
            except Exception:  # pragma: no cover - получатель не должен ломать проверку
                pass
        return alert

    def trigger_kind(self, conn, kind: str, key: str, title: str, text: str, payload: dict | None = None):
        """Оповещение от раздела приложения: только если есть включённое правило этого типа."""
        out = []
        for rule in self.rules(conn, kind, enabled_only=True):
            a = self.trigger(conn, kind, key, title, text, payload, rule["id"])
            if a:
                out.append(a)
        return out

    def list_alerts(self, conn, since_id: int = 0, limit: int = 200) -> list[dict]:
        rows = conn.execute(
            "SELECT id, ts, rule_id, kind, title, text, payload, seen FROM alerts WHERE id > ? "
            "ORDER BY id DESC LIMIT ?", (since_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            d["seen"] = bool(d["seen"])
            out.append(d)
        return out

    def mark_seen(self, conn, ids=None) -> None:
        if ids:
            conn.executemany("UPDATE alerts SET seen = 1 WHERE id = ?", [(int(i),) for i in ids])
        else:
            conn.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")

    def cleanup(self, conn) -> int:
        return conn.execute("DELETE FROM alerts WHERE ts < ?", (int(self.clock()) - KEEP_DAYS * 86400,)).rowcount

    # --- проверка рыночных правил --------------------------------------
    def check_market(self, conn, items: set[str], tax: float,
                     filter_factory: Callable[[dict], Callable[[str], bool]] | None = None,
                     touched: set[tuple] | None = None) -> list[dict]:
        """``touched`` — пары (предмет, рынок) из свежего пакета: ценовые правила и недооценённые
        лоты проверяются только по ним, чтобы старые цены не поднимали оповещения повторно."""
        self._touched = touched
        rules = [r for r in self.rules(conn, enabled_only=True) if r["kind"] in MARKET_KINDS]
        if not rules or not items:
            return []
        now = int(self.clock())
        fired = []
        for rule in rules:
            p = rule["params"]
            max_age = float(p.get("max_age") or 6)
            orders = db.load_orders(conn, now - int(max_age * 3600), None, now, items=items)
            if filter_factory:
                ok = filter_factory(p)
                orders = [o for o in orders if ok(o["item_id"])]
            handler = getattr(self, "_check_" + rule["kind"])
            fired.extend(a for a in handler(conn, rule, orders, tax) if a)
        return fired

    def _price_rule(self, conn, rule, orders, below: bool):
        p = rule["params"]
        item = p.get("item")
        locs = set(p.get("locations") or [])
        quality = int(p.get("quality") or 0)
        side = p.get("side") or ("sell" if below else "buy")
        target = float(p.get("price") or 0)
        want = "offer" if side == "sell" else "request"
        best = {}
        for o in orders:
            if o["item_id"] != item or o["auction_type"] != want:
                continue
            if self._touched is not None and (o["item_id"], o["location"]) not in self._touched:
                continue
            if locs and o["location"] not in locs:
                continue
            if quality and o["quality"] != quality:
                continue
            key = (o["location"], o["quality"])
            cur = best.get(key)
            if cur is None:
                best[key] = o
            elif want == "offer" and o["price"] < cur["price"]:
                best[key] = o
            elif want == "request" and o["price"] > cur["price"]:
                best[key] = o
        out = []
        for (loc, q), o in best.items():
            hit = o["price"] <= target if below else o["price"] >= target
            if not hit:
                continue
            what = "продают" if want == "offer" else "покупают"
            out.append(self.trigger(
                conn, rule["kind"], f"{item}:{loc}:{q}:{o['price']}",
                f"{self.name_of(item)}: {o['price']:,}".replace(",", " "),
                f"{self.loc_name(loc)}, качество {q}: {what} по {o['price']:,} (порог {int(target):,})".replace(",", " "),
                {"item_id": item, "location": loc, "quality": q, "price": o["price"]}, rule["id"]))
        return out

    def _check_price_below(self, conn, rule, orders, _tax):
        return self._price_rule(conn, rule, orders, True)

    def _check_price_above(self, conn, rule, orders, _tax):
        return self._price_rule(conn, rule, orders, False)

    def _check_deal(self, conn, rule, orders, tax):
        p = rule["params"]
        params = DealParams(
            premium=True, tax=tax,
            buy_mode=p.get("buy", "instant"), sell_mode=p.get("sell", "instant"),
            sources=tuple(p.get("src") or ()), destinations=tuple(p.get("dst") or ()),
            min_profit=float(p.get("min_profit") or 0), min_margin=float(p.get("min_margin") or 0),
            min_total_profit=float(p.get("min_total") or 0))
        out = []
        for d in find_deals(orders, params)[:5]:
            out.append(self.trigger(
                conn, "deal", f"{d.item_id}:{d.source}:{d.destination}:{d.sell_quality}:{round(d.unit_profit, -2)}",
                f"Сделка: {self.name_of(d.item_id)}",
                f"{self.loc_name(d.source)} → {self.loc_name(d.destination)}: прибыль {round(d.unit_profit):,}/шт, "
                f"итого {round(d.total_profit):,} ({d.margin:.0f}%)".replace(",", " "),
                {"item_id": d.item_id, "source": d.source, "destination": d.destination,
                 "profit": d.total_profit}, rule["id"]))
        return out

    def _check_underpriced(self, conn, rule, orders, tax):
        p = rule["params"]
        locs = set(p.get("locations") or [])
        offers = [o for o in orders if o["auction_type"] == "offer" and (not locs or o["location"] in locs)
                  and (self._touched is None or (o["item_id"], o["location"]) in self._touched)]
        refs = db.reference_prices(conn, int(self.clock()) - 7 * 86400, getattr(self, "index", {}))
        out = []
        for r in underpriced(offers, refs, tax)[:5]:
            if r["discount"] < float(p.get("min_discount") or 20) or r["profit"] < float(p.get("min_profit") or 0):
                continue
            out.append(self.trigger(
                conn, "underpriced", f"{r['item_id']}:{r['location']}:{r['quality']}:{r['price']}",
                f"Дёшево: {self.name_of(r['item_id'])}",
                f"{self.loc_name(r['location'])}: {r['price']:,} вместо ~{round(r['ref_price']):,} "
                f"(−{r['discount']:.0f}%, прибыль {round(r['profit']):,}/шт)".replace(",", " "),
                {"item_id": r["item_id"], "location": r["location"], "price": r["price"]}, rule["id"]))
        return out
