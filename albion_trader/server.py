"""HTTP-сервер: приёмник данных albiondata-client, REST API и веб-интерфейс."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db
from .deals import (TAX_NO_PREMIUM, TAX_PREMIUM, DealParams, bm_demand, fast_sell_table, find_deals, flip_table,
                    price_table, underpriced)
from . import alerts as alerts_mod
from . import mytrades as mytrades_mod
from .alerts import AlertEngine
from .mytrades import MyTrades
from .capture.albion import AlbionState, load_opcodes
from .capture.sniffer import Sniffer
from .gamedata import GameData
from .production import CraftParams, PriceBook, craft_table, enchant_table, farming_table, journal_table
from .items import ItemCatalog, enchant_of, tier_of
from .locations import DEFAULT_CITIES, MARKETS, market_info, normalize_location

log = logging.getLogger("albion_trader")

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 20 * 1024 * 1024
KNOWN_TOPIC_SUFFIXES = (".ingest", "marketnotifications", "skills")

# Чужие заказы старше этого не считаются при проверке «вас перебили».
OUTBID_FRESHNESS = 6 * 3600

# Значения настроек по умолчанию; хранятся в таблице settings.
DEFAULT_SETTINGS = {
    "premium": True,
    "station_fee": 400,          # серебро за 100 питания на станции
    "use_focus": False,
}


@dataclass
class AppConfig:
    db_path: Path
    items_path: Path
    token: str = ""
    retention_hours: float = 72
    cleanup_interval: int = 600
    capture: bool = True
    opcodes_path: Path | None = None
    record_path: str | None = None


class App:
    def __init__(self, config: AppConfig):
        self.config = config
        db.init_db(config.db_path)
        self.catalog = ItemCatalog.load(config.items_path)
        self.gamedata = GameData.load(Path(config.items_path).with_name("gamedata.json"))
        self.write_lock = threading.Lock()
        with self.conn() as conn:
            alerts_mod.init(conn)
        self.alerts = AlertEngine(self.conn, name_of=self.catalog.name,
                                  loc_name=lambda l: market_info(l)["name"])
        self.alerts.index = self.catalog.index
        self.albion = AlbionState(self.ingest, load_opcodes(config.opcodes_path))
        with self.conn() as conn:
            mytrades_mod.init(conn)
        self.mytrades = MyTrades(self.conn, self.write_lock, on_orders=self._after_my_orders)
        with self.conn() as conn:
            self.alerts.ensure_rule(conn, "outbid", "Мой заказ перебили")
        self.mytrades.attach(self.albion)
        self.albion.on("response:gold_market_get_average_info", self._gold_from_capture)
        self.sniffer: Sniffer | None = None

    def start_capture(self, open_sockets=None) -> bool:
        kwargs = {"open_sockets": open_sockets} if open_sockets else {}
        self.sniffer = Sniffer(self.albion, record_path=self.config.record_path, **kwargs)
        return self.sniffer.start()

    def capture_status(self) -> dict:
        st = dict(self.albion.stats)
        if self.sniffer is None:
            st.update(enabled=False, running=False, error=None, packets=0, last_packet_at=None)
        else:
            st.update(enabled=True, **self.sniffer.status)
            st["incomplete_messages"] = self.sniffer.parser.evicted_segments
        loc = st.get("location")
        if loc == "3003":
            # В зоне 3003 (город Карлеон) работает Чёрный рынок; обычный рынок — в зоне 3005.
            st["location_name"] = "Карлеон, город (Чёрный рынок)"
        else:
            st["location_name"] = market_info(normalize_location(loc))["name"] if loc else None
        return st

    @contextmanager
    def conn(self):
        conn = db.connect(self.config.db_path)
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    # --- приём данных -------------------------------------------------
    def ingest(self, topic: str, payload) -> int:
        with self.write_lock, self.conn() as conn:
            saved = db.ingest(conn, topic, payload)
            if saved and topic == "marketorders.ingest":
                orders = [o for o in payload.get("Orders") or [] if isinstance(o, dict)]
                items = {o.get("ItemTypeId") for o in orders}
                touched = {(o.get("ItemTypeId"), normalize_location(o.get("LocationId"))) for o in orders}
                try:
                    self.alerts.check_market(conn, items, self.current_tax(conn), self._rule_filter, touched)
                    self.check_outbid(conn, items)
                except Exception:  # pragma: no cover - ошибка правила не мешает сбору
                    log.exception("Ошибка проверки оповещений")
            return saved

    def _gold_from_capture(self, params: dict) -> None:
        prices, stamps = params.get(0), params.get(1)
        if isinstance(prices, list) and isinstance(stamps, list) and prices:
            self.ingest("goldprices.ingest", {"Prices": prices, "Timestamps": stamps})

    def api_gold(self, q) -> dict:
        now = int(time.time())
        since = now - int(_float(q.get("days"), 30) * 86400)
        with self.conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT ts, price FROM gold_prices WHERE ts >= ? ORDER BY ts", (since,))]
            last = conn.execute("SELECT ts, price FROM gold_prices ORDER BY ts DESC LIMIT 1").fetchone()
            sold = conn.execute("SELECT COALESCE(SUM(total), 0) FROM my_trades WHERE kind = 'sell' AND ts >= ?",
                                (now - 30 * 86400,)).fetchone()[0]
        return {"now": now, "prices": rows, "current": dict(last) if last else None,
                "sold_30d": round(sold or 0, 2)}

    def _after_my_orders(self) -> None:
        with self.write_lock, self.conn() as conn:
            self.check_outbid(conn)

    def current_tax(self, conn=None) -> float:
        settings = {**DEFAULT_SETTINGS, **(db.get_settings(conn) if conn else self.settings())}
        return TAX_PREMIUM if settings.get("premium", True) else TAX_NO_PREMIUM

    def _rule_filter(self, params: dict):
        join = lambda v: ",".join(str(x) for x in v) if isinstance(v, list) else (v or "")
        return self._item_filter({"q": params.get("q") or "", "tiers": join(params.get("tiers")),
                                  "enchants": join(params.get("enchants"))})

    def cleanup(self) -> dict:
        with self.write_lock, self.conn() as conn:
            res = db.cleanup(conn, self.config.retention_hours)
            res["alerts"] = self.alerts.cleanup(conn)
            return res

    # --- мои сделки -----------------------------------------------------
    def _my_order_status(self, conn, rows: list[dict]) -> dict:
        now = int(time.time())
        items = {r["item_id"] for r in rows}
        if not items:
            return {}
        market = db.load_orders(conn, now - OUTBID_FRESHNESS, None, now, items=items)
        return mytrades_mod.outbid_status(rows, market)

    def check_outbid(self, conn, items: set | None = None) -> list:
        rows = [r for r in mytrades_mod.open_orders(conn) if r["location"] and (items is None or r["item_id"] in items)]
        fired = []
        for r in rows:
            st = self._my_order_status(conn, [r]).get(r["id"])
            if not st or not st["outbid"]:
                continue
            what = "предложение" if r["auction_type"] == "offer" else "заказ на покупку"
            fired += self.alerts.trigger_kind(
                conn, "outbid", f"{r['id']}:{st['best_price']}",
                f"Перебили: {self.catalog.name(r['item_id'])}",
                f"Ваше {what} за {r['price']:,} в {market_info(r['location'])['name']}: лучший уже "
                f"{st['best_price']:,}, ставьте {st['suggested_price']:,}".replace(",", " "),
                {"item_id": r["item_id"], "location": r["location"], "order_id": r["id"]})
        return fired

    def api_my_orders(self, q) -> dict:
        with self.conn() as conn:
            rows = mytrades_mod.open_orders(conn)
            status = self._my_order_status(conn, rows)
        for r in rows:
            r.update(status.get(r["id"], {}))
        return {"now": int(time.time()), "rows": self._named(rows, q, 1000),
                "character": self.albion.character_name}

    def api_my_trades(self, q) -> dict:
        now = int(time.time())
        since = now - int(_float(q.get("days"), 30) * 86400)
        with self.conn() as conn:
            rows = mytrades_mod.trades(conn, since)
        report = mytrades_mod.summary(rows, self.current_tax())
        self._named(report["items"], q, 10**6)
        return {"now": now, "trades": self._named(rows, q, 2000), **report}

    # --- оповещения -----------------------------------------------------
    def api_alerts(self, q) -> dict:
        with self.conn() as conn:
            items = self.alerts.list_alerts(conn, int(_float(q.get("since"), 0)), int(_float(q.get("limit"), 200)))
            unseen = conn.execute("SELECT COUNT(*) FROM alerts WHERE seen = 0").fetchone()[0]
        return {"now": int(time.time()), "alerts": items, "unseen": unseen}

    def api_alerts_seen(self, _q, body) -> dict:
        with self.write_lock, self.conn() as conn:
            self.alerts.mark_seen(conn, body.get("ids") if isinstance(body, dict) else None)
        return {"ok": True}

    def api_alert_rules(self, _q) -> dict:
        with self.conn() as conn:
            return {"rules": self.alerts.rules(conn), "kinds": alerts_mod.KINDS}

    def api_alert_rules_post(self, _q, body) -> dict:
        if not isinstance(body, dict):
            raise ApiError("ожидается объект")
        action = body.get("action", "save")
        with self.write_lock, self.conn() as conn:
            if action == "save":
                try:
                    rule_id = self.alerts.save_rule(conn, body.get("rule") or {})
                except ValueError as e:
                    raise ApiError(str(e)) from e
            elif action == "delete":
                rule_id = int(body.get("id") or 0)
                conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
            elif action == "toggle":
                rule_id = int(body.get("id") or 0)
                conn.execute("UPDATE alert_rules SET enabled = ? WHERE id = ?",
                             (int(bool(body.get("enabled"))), rule_id))
            else:
                raise ApiError(f"неизвестное действие: {action}")
        return {"ok": True, "id": rule_id, **self.api_alert_rules({})}

    # --- настройки ------------------------------------------------------
    def settings(self) -> dict:
        with self.conn() as conn:
            return {**DEFAULT_SETTINGS, **db.get_settings(conn)}

    def api_settings(self, _q) -> dict:
        return self.settings()

    def api_settings_post(self, _q, body) -> dict:
        if not isinstance(body, dict):
            raise ApiError("ожидается объект настроек")
        with self.write_lock, self.conn() as conn:
            db.set_settings(conn, body)
        return self.settings()

    # --- API ------------------------------------------------------------
    def api_locations(self, _q) -> dict:
        with self.conn() as conn:
            seen = [r[0] for r in conn.execute("SELECT DISTINCT location FROM orders")]
        keys = list(MARKETS) + sorted(k for k in seen if k not in MARKETS)
        return {"locations": [market_info(k) for k in keys], "default_cities": DEFAULT_CITIES}

    def api_status(self, _q) -> dict:
        with self.conn() as conn:
            topics = [dict(r) for r in conn.execute(
                "SELECT topic, last_at, batches, records FROM ingest_stats ORDER BY last_at DESC")]
            per_loc = [dict(r) for r in conn.execute(
                """SELECT location, auction_type, COUNT(*) AS orders,
                          COUNT(DISTINCT item_id) AS items, MAX(seen_at) AS last_seen
                   FROM orders GROUP BY location, auction_type""")]
            total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        for r in per_loc:
            r["name"] = market_info(r["location"])["name"]
        return {
            "now": int(time.time()), "topics": topics, "locations": per_loc,
            "total_orders": total, "history_points": history,
            "items_catalog": len(self.catalog),
            "gamedata_recipes": len(self.gamedata.recipes),
            "capture": self.capture_status(),
            "ingest_path": f"/{self.config.token}" if self.config.token else "",
        }

    def _item_filter(self, q):
        text = (q.get("q") or "").strip().lower()
        tiers = {int(t) for t in _split(q.get("tiers")) if t.isdigit()}
        enchants = {int(e) for e in _split(q.get("enchants")) if e.isdigit()}

        def ok(item_id: str) -> bool:
            if tiers and tier_of(item_id) not in tiers:
                return False
            if enchants and enchant_of(item_id) not in enchants:
                return False
            if text and not all(w in self.catalog.search_text(item_id) for w in text.split()):
                return False
            return True
        return ok

    def api_deals(self, q) -> dict:
        now = int(time.time())
        max_age = _float(q.get("max_age"), 6)
        sources = _split(q.get("src")) or DEFAULT_CITIES
        destinations = _split(q.get("dst")) or DEFAULT_CITIES + ["black_market"]
        params = DealParams(
            premium=q.get("premium", "1") != "0",
            buy_mode="order" if q.get("buy") == "order" else "instant",
            sell_mode="order" if q.get("sell") == "order" else "instant",
            sources=tuple(sources), destinations=tuple(destinations),
            min_profit=_float(q.get("min_profit"), 0),
            min_margin=_float(q.get("min_margin"), 0),
            min_total_profit=_float(q.get("min_total"), 0),
            tax=None if not q.get("tax") else _float(q.get("tax"), 0) / 100,
        )
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(max_age * 3600),
                                    sorted(set(sources) | set(destinations)), now)
            volumes = db.load_daily_volumes(conn, self.catalog.index, now)
        ok = self._item_filter(q)
        orders = [o for o in orders if ok(o["item_id"])]
        deals = find_deals(orders, params)
        limit = int(_float(q.get("limit"), 300))
        lang = q.get("lang", "ru")
        out = []
        for d in deals[:limit]:
            row = d.to_dict()
            row["name"] = self.catalog.name(d.item_id, lang)
            row["tier"] = tier_of(d.item_id)
            row["enchant"] = enchant_of(d.item_id)
            row["daily_volume"] = volumes.get((d.item_id, d.destination, d.sell_quality))
            out.append(row)
        return {"now": now, "count": len(deals), "deals": out,
                "tax": params.sales_tax, "setup_fee": params.setup_fee}

    def api_fastsell(self, q) -> dict:
        now = int(time.time())
        max_age = _float(q.get("max_age"), 6)
        locs = _split(q.get("locs")) or DEFAULT_CITIES + ["black_market"]
        base = q.get("base") or None
        if q.get("tax"):
            tax = _float(q.get("tax"), 0) / 100
        else:
            tax = TAX_PREMIUM if q.get("premium", "1") != "0" else TAX_NO_PREMIUM
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(max_age * 3600), locs, now)
            volumes = db.load_daily_volumes(conn, self.catalog.index, now)
        ok = self._item_filter(q)
        orders = [o for o in orders if o["auction_type"] == "request" and ok(o["item_id"])]
        rows = fast_sell_table(orders, locs, tax, base, int(_float(q.get("min_markets"), 2)))
        min_gain = _float(q.get("min_gain"), 0)
        key = "gain_vs_base" if base else "spread"
        rows = [r for r in rows if (r[key] or 0) >= min_gain]
        lang = q.get("lang", "ru")
        limit = int(_float(q.get("limit"), 300))
        for r in rows[:limit]:
            r["name"] = self.catalog.name(r["item_id"], lang)
            r["daily_volume"] = volumes.get((r["item_id"], r["best_location"], r["quality"]))
        return {"now": now, "count": len(rows), "tax": tax, "locations": locs, "base": base,
                "rows": rows[:limit]}

    def _tax(self, q) -> float:
        if q.get("tax"):
            return _float(q.get("tax"), 0) / 100
        return TAX_PREMIUM if q.get("premium", "1") != "0" else TAX_NO_PREMIUM

    def _named(self, rows: list[dict], q, limit_default: int = 300) -> list[dict]:
        lang = q.get("lang", "ru")
        rows = rows[:int(_float(q.get("limit"), limit_default))]
        for r in rows:
            r["name"] = self.catalog.name(r["item_id"], lang)
        return rows

    def api_flips(self, q) -> dict:
        now = int(time.time())
        locs = [l for l in (_split(q.get("locs")) or DEFAULT_CITIES) if l != "black_market"]
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(_float(q.get("max_age"), 6) * 3600), locs, now)
            volumes = db.load_daily_volumes(conn, self.catalog.index, now)
        ok = self._item_filter(q)
        rows = flip_table([o for o in orders if ok(o["item_id"])], self._tax(q), volumes=volumes)
        min_margin, min_profit = _float(q.get("min_margin"), 0), _float(q.get("min_profit"), 0)
        min_volume = _float(q.get("min_volume"), 0)
        rows = [r for r in rows if r["margin"] >= min_margin and r["profit"] >= min_profit
                and (not min_volume or (r["daily_volume"] or 0) >= min_volume)]
        return {"now": now, "count": len(rows), "rows": self._named(rows, q)}

    def api_underpriced(self, q) -> dict:
        now = int(time.time())
        locs = [l for l in (_split(q.get("locs")) or DEFAULT_CITIES) if l != "black_market"]
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(_float(q.get("max_age"), 2) * 3600), locs, now)
            refs = db.reference_prices(conn, now - 7 * 86400, self.catalog.index)
        ok = self._item_filter(q)
        offers = [o for o in orders if o["auction_type"] == "offer" and ok(o["item_id"])]
        rows = underpriced(offers, refs, self._tax(q))
        min_discount, min_profit = _float(q.get("min_discount"), 20), _float(q.get("min_profit"), 0)
        rows = [r for r in rows if r["discount"] >= min_discount and r["profit"] >= min_profit]
        return {"now": now, "count": len(rows), "rows": self._named(rows, q)}

    def api_bm_demand(self, q) -> dict:
        now = int(time.time())
        days = max(_float(q.get("days"), 7), 0.1)
        since = now - int(days * 86400)
        with self.conn() as conn:
            snaps = [dict(r) for r in conn.execute(
                "SELECT item_id, quality, ts, buy_max, buy_amount FROM price_snapshots "
                "WHERE location = 'black_market' AND ts >= ?", (since,))]
            sales = {}
            for r in conn.execute(
                    """SELECT albion_id, quality, SUM(item_amount), SUM(silver_amount) FROM history
                       WHERE location = 'black_market' AND timescale = 1 AND ts >= ? AND item_amount > 0
                       GROUP BY albion_id, quality""", (since,)):
                item_id = self.catalog.index.get(str(r[0]))
                if item_id and r[2]:
                    sales[(item_id, r[1])] = (r[2], r[3] / r[2] / db.PRICE_SCALE)
            offers = [o for o in db.load_orders(conn, now - int(_float(q.get("max_age"), 24) * 3600),
                                                DEFAULT_CITIES, now) if o["auction_type"] == "offer"]
        ok = self._item_filter(q)
        rows = bm_demand([s for s in snaps if ok(s["item_id"])],
                         {k: v for k, v in sales.items() if ok(k[0])}, offers, self._tax(q), days)
        min_margin = q.get("min_margin")
        if min_margin not in (None, ""):
            rows = [r for r in rows if r["margin"] is not None and r["margin"] >= _float(min_margin, 0)]
        return {"now": now, "count": len(rows), "rows": self._named(rows, q)}

    def _no_gamedata(self) -> dict:
        return {"now": int(time.time()), "count": 0, "rows": [], "no_gamedata": True}

    def api_craft(self, q) -> dict:
        if not self.gamedata:
            return self._no_gamedata()
        now = int(time.time())
        buy = q.get("buy_market") or "martlock"
        sell = q.get("sell_market") or buy
        params = CraftParams(
            kind=q.get("kind") if q.get("kind") in ("refine", "transmute") else "craft",
            buy_market=buy, sell_market=sell, craft_city=q.get("craft_city") or buy,
            buy_mode="order" if q.get("buy_mode") == "order" else "instant",
            sell_mode="order" if q.get("sell_mode") == "order" else "instant",
            focus=q.get("focus") == "1", tax=self._tax(q),
            station_fee=_float(q.get("station_fee"), 0), category=q.get("category") or "",
            include_incomplete=q.get("incomplete") == "1")
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(_float(q.get("max_age"), 24) * 3600),
                                    sorted({buy, sell}), now)
            volumes = db.load_daily_volumes(conn, self.catalog.index, now)
        rows = craft_table(self.gamedata, PriceBook(orders), params, self._item_filter(q), volumes)
        min_margin = q.get("min_margin")
        if min_margin not in (None, ""):
            rows = [r for r in rows if r["margin"] is not None and r["margin"] >= _float(min_margin, 0)]
        rows = self._named(rows, q, 400)
        for r in rows:
            for m in r["materials"]:
                m["name"] = self.catalog.name(m["item_id"])
            r["missing_names"] = [self.catalog.name(i) for i in r["missing"]]
        return {"now": now, "count": len(rows), "rows": rows}

    def api_enchant(self, q) -> dict:
        if not self.gamedata:
            return self._no_gamedata()
        now = int(time.time())
        buy = q.get("buy_market") or "martlock"
        sell = q.get("sell_market") or buy
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(_float(q.get("max_age"), 24) * 3600), sorted({buy, sell}), now)
        ok = self._item_filter({k: v for k, v in q.items() if k != "enchants"})
        qualities = [int(x) for x in _split(q.get("qualities")) if x.isdigit()] or [1, 2, 3, 4, 5]
        rows = enchant_table(self.gamedata, PriceBook(orders), buy, sell,
                             "order" if q.get("buy_mode") == "order" else "instant",
                             "order" if q.get("sell_mode") == "order" else "instant",
                             self._tax(q), ok, qualities)
        rows = [r for r in rows if r["profit"] >= _float(q.get("min_profit"), -10**12)]
        rows = self._named(rows, q, 400)
        for r in rows:
            for m in r["materials"]:
                m["name"] = self.catalog.name(m["item_id"])
        return {"now": now, "count": len(rows), "rows": rows}

    def api_journals(self, q) -> dict:
        if not self.gamedata:
            return self._no_gamedata()
        now = int(time.time())
        buy = q.get("buy_market") or "martlock"
        sell = q.get("sell_market") or buy
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(_float(q.get("max_age"), 24) * 3600), sorted({buy, sell}), now)
        rows = journal_table(self.gamedata, PriceBook(orders), buy, sell, self._tax(q),
                             _float(q.get("happiness"), 100) / 100, self._item_filter(q))
        return {"now": now, "count": len(rows), "rows": self._named(rows, q, 400)}

    def api_farming(self, q) -> dict:
        if not self.gamedata:
            return self._no_gamedata()
        now = int(time.time())
        buy = q.get("buy_market") or "martlock"
        sell = q.get("sell_market") or buy
        with self.conn() as conn:
            orders = db.load_orders(conn, now - int(_float(q.get("max_age"), 24) * 3600), sorted({buy, sell}), now)
        rows = farming_table(self.gamedata, PriceBook(orders), buy, sell, self._tax(q),
                             focus=q.get("focus") == "1", yield_mult=_float(q.get("yield_mult"), 100) / 100,
                             feed_cost=_float(q.get("feed_cost"), 0), item_ok=self._item_filter(q))
        if q.get("kind") in ("plant", "animal"):
            rows = [r for r in rows if r["kind"] == q.get("kind")]
        rows = self._named(rows, q, 400)
        for r in rows:
            r["product_name"] = self.catalog.name(r["product_id"]) if r["product_id"] else ""
            for part in r["parts"]:
                part["name"] = self.catalog.name(part["item_id"])
        return {"now": now, "count": len(rows), "rows": rows}

    def api_history(self, q) -> dict:
        item_id = (q.get("item") or "").strip()
        now = int(time.time())
        since = now - int(_float(q.get("days"), 7) * 86400)
        location = q.get("location") or None
        quality = int(_float(q.get("quality"), 0)) or None
        albion_id = self.catalog.reverse_index.get(item_id)
        with self.conn() as conn:
            snaps = db.load_snapshots(conn, item_id, since, location, quality)
            sales = db.load_sales(conn, albion_id, since, location, quality) if albion_id is not None else []
        return {"now": now, "item_id": item_id, "name": self.catalog.name(item_id, q.get("lang", "ru")),
                "snapshots": snaps, "sales": sales}

    def api_prices(self, q) -> dict:
        item_id = (q.get("item") or "").strip()
        now = int(time.time())
        max_age = _float(q.get("max_age"), 72)
        with self.conn() as conn:
            rows = [dict(r) for r in conn.execute(
                """SELECT item_id, location, quality, price, amount, auction_type, seen_at
                   FROM orders WHERE item_id = ? AND seen_at >= ?
                   AND (expires IS NULL OR expires >= ?)""",
                (item_id, now - int(max_age * 3600), now))]
            volumes = db.load_daily_volumes(conn, self.catalog.index, now)
        table = price_table(rows)
        for r in table:
            r["name"] = market_info(r["location"])["name"]
            r["daily_volume"] = volumes.get((item_id, r["location"], r["quality"]))
        return {"now": now, "item_id": item_id,
                "name": self.catalog.name(item_id, q.get("lang", "ru")), "rows": table}

    def api_items(self, q) -> dict:
        ok = self._item_filter(q)
        lang = q.get("lang", "ru")
        limit = int(_float(q.get("limit"), 50))
        if q.get("recent"):
            with self.conn() as conn:
                rows = conn.execute(
                    """SELECT item_id, MAX(seen_at) AS last, COUNT(DISTINCT location) AS markets,
                              COUNT(*) AS orders
                       FROM orders GROUP BY item_id ORDER BY last DESC""").fetchall()
            out = [{"item_id": r["item_id"], "name": self.catalog.name(r["item_id"], lang),
                    "last_seen": r["last"], "markets": r["markets"], "orders": r["orders"]}
                   for r in rows if ok(r["item_id"])]
            return {"now": int(time.time()), "items": out[:limit]}
        with self.conn() as conn:
            ids = [r[0] for r in conn.execute("SELECT DISTINCT item_id FROM orders")]
        found = sorted((i for i in ids if ok(i)), key=lambda i: self.catalog.name(i, lang))
        return {"items": [{"item_id": i, "name": self.catalog.name(i, lang)} for i in found[:limit]]}


class ApiError(Exception):
    """Ошибка запроса к API (400)."""


def _split(value) -> list[str]:
    return [v for v in (value or "").split(",") if v]


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def make_handler(app: App):
    api = {
        "/api/locations": app.api_locations,
        "/api/status": app.api_status,
        "/api/deals": app.api_deals,
        "/api/prices": app.api_prices,
        "/api/fastsell": app.api_fastsell,
        "/api/settings": app.api_settings,
        "/api/flips": app.api_flips,
        "/api/history": app.api_history,
        "/api/underpriced": app.api_underpriced,
        "/api/bm-demand": app.api_bm_demand,
        "/api/craft": app.api_craft,
        "/api/enchant": app.api_enchant,
        "/api/journals": app.api_journals,
        "/api/farming": app.api_farming,
        "/api/alerts": app.api_alerts,
        "/api/my/orders": app.api_my_orders,
        "/api/gold": app.api_gold,
        "/api/my/trades": app.api_my_trades,
        "/api/alert-rules": app.api_alert_rules,
        "/api/items": app.api_items,
    }

    post_api = {
        "/api/settings": app.api_settings_post,
        "/api/alerts/seen": app.api_alerts_seen,
        "/api/alert-rules": app.api_alert_rules_post,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "AlbionTrader"

        def log_message(self, fmt, *args):  # noqa: N802 - имя из BaseHTTPRequestHandler
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in api:
                q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                try:
                    self._json(200, api[url.path](q))
                except ApiError as e:
                    self._json(400, {"error": str(e)})
                except Exception as e:  # pragma: no cover - защитный путь
                    log.exception("API error")
                    self._json(500, {"error": str(e)})
                return
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            path = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in path.parents or not path.is_file():
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        def _api_post(self, path: str) -> None:
            # Защита от запросов со сторонних сайтов: только JSON и только с этого же адреса.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                self._json(403, {"error": "forbidden origin"})
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(415, {"error": "expected application/json"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:
                self._json(400, {"error": "bad body size"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "invalid json"})
                return
            q = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                self._json(200, post_api[path](q, body))
            except ApiError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # pragma: no cover
                log.exception("API error")
                self._json(500, {"error": str(e)})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in post_api:
                self._api_post(path)
                return
            # albiondata-client шлёт POST <базовый URL>/<топик>, например
            # http://127.0.0.1:8484/marketorders.ingest или, с токеном,
            # http://127.0.0.1:8484/<токен>/marketorders.ingest
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if not parts:
                self._json(404, {"error": "not found"})
                return
            topic, prefix = parts[-1], parts[:-1]
            expected = [app.config.token] if app.config.token else []
            if prefix != expected:
                self._json(403, {"error": "forbidden"})
                return
            if not topic.endswith(KNOWN_TOPIC_SUFFIXES):
                self._json(404, {"error": "unknown topic"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json(400, {"error": "bad body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "invalid json"})
                return
            saved = app.ingest(topic, payload)
            if saved:
                log.info("Получено %s: %d записей", topic, saved)
            self._json(200, {"ok": True, "saved": saved})

    return Handler


def _cleanup_loop(app: App, stop: threading.Event):
    while not stop.wait(app.config.cleanup_interval):
        try:
            res = app.cleanup()
            if any(res.values()):
                log.info("Очистка: %s", res)
        except Exception:  # pragma: no cover
            log.exception("cleanup failed")


def serve(config: AppConfig, host: str, port: int) -> None:
    app = App(config)
    app.cleanup()
    if config.capture:
        app.start_capture()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    stop = threading.Event()
    threading.Thread(target=_cleanup_loop, args=(app, stop), daemon=True).start()
    base = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    ingest_url = base + (f"/{config.token}" if config.token else "")
    log.info("Интерфейс:           %s", base)
    if app.sniffer and app.sniffer.status["running"]:
        log.info("Встроенный сборщик работает — откройте рынок в игре.")
    log.info("Внешний клиент (необязательно): albiondata-client -i %s", ingest_url)
    if not len(app.catalog):
        log.info("Названия предметов не загружены: python -m albion_trader update-items")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if app.sniffer:
            app.sniffer.stop()
        httpd.server_close()
