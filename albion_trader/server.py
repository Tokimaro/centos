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
from .deals import TAX_NO_PREMIUM, TAX_PREMIUM, DealParams, fast_sell_table, find_deals, price_table
from .capture.albion import AlbionState, load_opcodes
from .capture.sniffer import Sniffer
from .items import ItemCatalog, enchant_of, tier_of
from .locations import DEFAULT_CITIES, MARKETS, market_info, normalize_location

log = logging.getLogger("albion_trader")

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 20 * 1024 * 1024
KNOWN_TOPIC_SUFFIXES = (".ingest", "marketnotifications", "skills")


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
        self.write_lock = threading.Lock()
        self.albion = AlbionState(self.ingest, load_opcodes(config.opcodes_path))
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
            return db.ingest(conn, topic, payload)

    def cleanup(self) -> dict:
        with self.write_lock, self.conn() as conn:
            return db.cleanup(conn, self.config.retention_hours)

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
        "/api/items": app.api_items,
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

        def do_POST(self):  # noqa: N802
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
