"""Локальное хранилище (SQLite) и разбор данных, присланных albiondata-client."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .locations import LEGACY_KEYS, normalize_location

# Клиент передаёт цены умноженными на 10 000.
PRICE_SCALE = 10_000

# Разница между эпохой .NET (0001-01-01) и Unix-эпохой, в тиках по 100 нс.
_DOTNET_EPOCH_TICKS = 621_355_968_000_000_000

# Заказы, увиденные раньше чем за столько секунд до нового снимка того же
# стакана и оказавшиеся «лучше» всех заказов в снимке, считаются исчезнувшими
# (купленными/отменёнными). Задержка нужна, чтобы просмотр второй страницы
# рынка не стирал только что сохранённую первую.
PRUNE_GRACE_SECONDS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY,
    item_id      TEXT    NOT NULL,
    location     TEXT    NOT NULL,
    raw_location TEXT,
    quality      INTEGER NOT NULL,
    enchant      INTEGER NOT NULL,
    price        INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    auction_type TEXT    NOT NULL,
    expires      INTEGER,
    seen_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_book ON orders(item_id, location, quality, auction_type);
CREATE INDEX IF NOT EXISTS idx_orders_seen ON orders(seen_at);

CREATE TABLE IF NOT EXISTS history (
    albion_id     INTEGER NOT NULL,
    location      TEXT    NOT NULL,
    quality       INTEGER NOT NULL,
    timescale     INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    item_amount   INTEGER NOT NULL,
    silver_amount INTEGER NOT NULL,
    seen_at       INTEGER NOT NULL,
    PRIMARY KEY (albion_id, location, quality, timescale, ts)
);

CREATE TABLE IF NOT EXISTS gold_prices (
    ts    INTEGER PRIMARY KEY,
    price INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    item_id     TEXT    NOT NULL,
    location    TEXT    NOT NULL,
    quality     INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    sell_min    INTEGER,
    sell_amount INTEGER,
    buy_max     INTEGER,
    buy_amount  INTEGER,
    PRIMARY KEY (item_id, location, quality, ts)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_stats (
    topic    TEXT PRIMARY KEY,
    last_at  INTEGER NOT NULL,
    batches  INTEGER NOT NULL,
    records  INTEGER NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: str | Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        with conn:
            for old, new in LEGACY_KEYS.items():
                conn.execute("UPDATE orders SET location=? WHERE location=?", (new, old))
                conn.execute("UPDATE OR REPLACE history SET location=? WHERE location=?", (new, old))
    finally:
        conn.close()


def parse_expires(value) -> int | None:
    """ISO-дата окончания заказа (UTC, до 7 знаков дробной части) -> unix."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def dotnet_ticks_to_unix(ticks: int) -> int:
    return int((int(ticks) - _DOTNET_EPOCH_TICKS) // 10_000_000)


def _bump_stats(conn: sqlite3.Connection, topic: str, records: int, now: int) -> None:
    conn.execute(
        """INSERT INTO ingest_stats(topic, last_at, batches, records) VALUES (?, ?, 1, ?)
           ON CONFLICT(topic) DO UPDATE SET last_at=excluded.last_at,
               batches=batches+1, records=records+excluded.records""",
        (topic, now, records),
    )


def _normalize_order(o: dict, now: int) -> tuple | None:
    try:
        order_id = int(o["Id"])
        item_id = str(o["ItemTypeId"]).strip()
        price = round(int(o["UnitPriceSilver"]) / PRICE_SCALE)
        amount = int(o["Amount"])
        auction_type = str(o["AuctionType"]).lower()
    except (KeyError, TypeError, ValueError):
        return None
    location = normalize_location(o.get("LocationId"))
    if not item_id or not location or price <= 0 or amount <= 0:
        return None
    if auction_type not in ("offer", "request"):
        return None
    quality = int(o.get("QualityLevel") or 1)
    enchant = int(o.get("EnchantmentLevel") or 0)
    raw_location = None if o.get("LocationId") is None else str(o.get("LocationId"))
    return (order_id, item_id, location, raw_location, quality, enchant, price,
            amount, auction_type, parse_expires(o.get("Expires")), now)


def ingest_market_orders(conn: sqlite3.Connection, payload: dict, now: int | None = None) -> int:
    """Сохраняет пакет заказов (топик ``marketorders.ingest``). Возвращает число сохранённых."""
    now = int(now if now is not None else time.time())
    rows = [r for r in (_normalize_order(o, now) for o in (payload.get("Orders") or [])
                        if isinstance(o, dict)) if r]
    if not rows:
        _bump_stats(conn, "marketorders.ingest", 0, now)
        return 0

    # Для каждого стакана из пакета: предложения (offer) приходят от дешёвых к
    # дорогим, запросы (request) — от дорогих к дешёвым. Всё, что было лучше
    # лучшей цены нового снимка и увидено давно, уже исчезло с рынка.
    best: dict[tuple, int] = {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r[1], r[2], r[4], r[8])].append(r[6])
    for key, prices in groups.items():
        best[key] = min(prices) if key[3] == "offer" else max(prices)
    cutoff = now - PRUNE_GRACE_SECONDS
    for (item_id, location, quality, auction_type), price in best.items():
        cmp = "<" if auction_type == "offer" else ">"
        conn.execute(
            f"""DELETE FROM orders WHERE item_id=? AND location=? AND quality=?
                AND auction_type=? AND price {cmp} ? AND seen_at < ?""",
            (item_id, location, quality, auction_type, price, cutoff),
        )

    conn.executemany(
        """INSERT INTO orders(id, item_id, location, raw_location, quality, enchant, price,
                              amount, auction_type, expires, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET item_id=excluded.item_id, location=excluded.location,
               raw_location=excluded.raw_location, quality=excluded.quality,
               enchant=excluded.enchant, price=excluded.price, amount=excluded.amount,
               auction_type=excluded.auction_type, expires=excluded.expires,
               seen_at=excluded.seen_at""",
        rows,
    )
    _bump_stats(conn, "marketorders.ingest", len(rows), now)
    record_snapshots(conn, {(r[1], r[2], r[4]) for r in rows}, now)
    return len(rows)


# Снимок лучших цен пишется, если цена изменилась или с прошлого прошло столько секунд.
SNAPSHOT_INTERVAL = 600
# Заказы старше этого не участвуют в снимке (их могли уже выкупить).
SNAPSHOT_FRESHNESS = 6 * 3600


def record_snapshots(conn: sqlite3.Connection, keys, now: int) -> int:
    """Сохраняет лучшие цены затронутых стаканов (предмет, рынок, качество)."""
    written = 0
    for item_id, location, quality in keys:
        best = conn.execute(
            """SELECT
                 (SELECT MIN(price) FROM orders WHERE item_id=?1 AND location=?2 AND quality=?3
                    AND auction_type='offer' AND seen_at >= ?4),
                 (SELECT SUM(amount) FROM orders WHERE item_id=?1 AND location=?2 AND quality=?3
                    AND auction_type='offer' AND seen_at >= ?4),
                 (SELECT MAX(price) FROM orders WHERE item_id=?1 AND location=?2 AND quality=?3
                    AND auction_type='request' AND seen_at >= ?4),
                 (SELECT SUM(amount) FROM orders WHERE item_id=?1 AND location=?2 AND quality=?3
                    AND auction_type='request' AND seen_at >= ?4)""",
            (item_id, location, quality, now - SNAPSHOT_FRESHNESS)).fetchone()
        if best[0] is None and best[2] is None:
            continue
        last = conn.execute(
            """SELECT ts, sell_min, buy_max FROM price_snapshots
               WHERE item_id=? AND location=? AND quality=? ORDER BY ts DESC LIMIT 1""",
            (item_id, location, quality)).fetchone()
        if last and last[1] == best[0] and last[2] == best[2] and now - last[0] < SNAPSHOT_INTERVAL:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO price_snapshots
               (item_id, location, quality, ts, sell_min, sell_amount, buy_max, buy_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, location, quality, now, best[0], best[1], best[2], best[3]))
        written += 1
    return written


def load_snapshots(conn: sqlite3.Connection, item_id: str, since: int, location: str | None = None,
                   quality: int | None = None) -> list[dict]:
    sql = ("SELECT location, quality, ts, sell_min, sell_amount, buy_max, buy_amount FROM price_snapshots "
           "WHERE item_id = ? AND ts >= ?")
    args: list = [item_id, since]
    if location:
        sql += " AND location = ?"
        args.append(location)
    if quality:
        sql += " AND quality = ?"
        args.append(quality)
    return [dict(r) for r in conn.execute(sql + " ORDER BY ts", args)]


def reference_prices(conn: sqlite3.Connection, since: int, index_to_item: dict,
                     min_snapshots: int = 3) -> dict:
    """Опорные цены: медиана мин. цены продажи по снимкам (если снимков ≥ min_snapshots),
    иначе средняя цена сделок из истории. {(item, location, quality): (цена, источник, точек)}."""
    series: dict[tuple, list] = {}
    for r in conn.execute("SELECT item_id, location, quality, sell_min FROM price_snapshots "
                          "WHERE ts >= ? AND sell_min IS NOT NULL", (since,)):
        series.setdefault((r[0], r[1], r[2]), []).append(r[3])
    refs = {}
    for key, values in series.items():
        if len(values) >= min_snapshots:
            v = sorted(values)
            n = len(v)
            refs[key] = (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, "median", n)
    for r in conn.execute(
            """SELECT albion_id, location, quality, SUM(silver_amount) AS silver, SUM(item_amount) AS amount,
                      COUNT(*) AS points
               FROM history WHERE timescale = 1 AND ts >= ? AND item_amount > 0
               GROUP BY albion_id, location, quality""", (since,)):
        item_id = index_to_item.get(str(r["albion_id"]))
        key = (item_id, r["location"], r["quality"])
        if item_id and key not in refs and r["amount"]:
            refs[key] = (r["silver"] / r["amount"] / PRICE_SCALE, "sales", r["points"])
    return refs


def load_sales(conn: sqlite3.Connection, albion_id: int, since: int, location: str | None = None,
               quality: int | None = None) -> list[dict]:
    """Средняя цена и объём сделок по истории продаж (дневные точки, иначе часовые)."""
    sql = ("SELECT location, quality, timescale, ts, item_amount, silver_amount FROM history "
           "WHERE albion_id = ? AND ts >= ?")
    args: list = [albion_id, since]
    if location:
        sql += " AND location = ?"
        args.append(location)
    if quality:
        sql += " AND quality = ?"
        args.append(quality)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY ts", args)]
    daily = {(r["location"], r["quality"]) for r in rows if r["timescale"] == 1}
    out = []
    for r in rows:
        key = (r["location"], r["quality"])
        wanted = 1 if key in daily else 0
        if r["timescale"] != wanted or not r["item_amount"]:
            continue
        out.append({"location": r["location"], "quality": r["quality"], "ts": r["ts"],
                    "amount": r["item_amount"],
                    "avg_price": round(r["silver_amount"] / r["item_amount"] / PRICE_SCALE, 2)})
    return out


def ingest_market_history(conn: sqlite3.Connection, payload: dict, now: int | None = None) -> int:
    """Сохраняет историю продаж (топик ``markethistories.ingest``)."""
    now = int(now if now is not None else time.time())
    try:
        albion_id = int(payload["AlbionId"])
        location = normalize_location(payload.get("LocationId"))
        quality = int(payload.get("QualityLevel") or 0)
        timescale = int(payload.get("Timescale") or 0)
    except (KeyError, TypeError, ValueError):
        return 0
    if not location:
        return 0
    rows = []
    for h in payload.get("MarketHistories") or []:
        try:
            rows.append((albion_id, location, quality, timescale,
                         dotnet_ticks_to_unix(h["Timestamp"]), int(h["ItemAmount"]),
                         int(h["SilverAmount"]), now))
        except (KeyError, TypeError, ValueError):
            continue
    conn.executemany(
        """INSERT OR REPLACE INTO history(albion_id, location, quality, timescale, ts,
                                          item_amount, silver_amount, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    _bump_stats(conn, "markethistories.ingest", len(rows), now)
    return len(rows)


def ingest_gold_prices(conn: sqlite3.Connection, payload: dict, now: int | None = None) -> int:
    now = int(now if now is not None else time.time())
    prices = payload.get("Prices") or []
    stamps = payload.get("Timestamps") or []
    rows = []
    for price, ts in zip(prices, stamps):
        try:
            ts = int(ts)
            # Клиент может прислать тики .NET вместо unix-времени.
            if ts > 10**14:
                ts = dotnet_ticks_to_unix(ts)
            rows.append((ts, int(price)))
        except (TypeError, ValueError):
            continue
    conn.executemany("INSERT OR REPLACE INTO gold_prices(ts, price) VALUES (?, ?)", rows)
    _bump_stats(conn, "goldprices.ingest", len(rows), now)
    return len(rows)


def ingest(conn: sqlite3.Connection, topic: str, payload, now: int | None = None) -> int:
    """Маршрутизирует пакет по топику. Незнакомые топики только учитываются в статистике."""
    if not isinstance(payload, dict):
        payload = {}
    if topic == "marketorders.ingest":
        return ingest_market_orders(conn, payload, now)
    if topic == "markethistories.ingest":
        return ingest_market_history(conn, payload, now)
    if topic == "goldprices.ingest":
        return ingest_gold_prices(conn, payload, now)
    _bump_stats(conn, topic, 0, int(now if now is not None else time.time()))
    return 0


def get_settings(conn: sqlite3.Connection) -> dict:
    out = {}
    for key, value in conn.execute("SELECT key, value FROM settings"):
        try:
            out[key] = json.loads(value)
        except ValueError:
            continue
    return out


def set_settings(conn: sqlite3.Connection, values: dict) -> None:
    conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                     [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])


def cleanup(conn: sqlite3.Connection, retention_hours: float, now: int | None = None) -> dict:
    """Удаляет истёкшие заказы, слишком старые снимки и старую историю."""
    now = int(now if now is not None else time.time())
    expired = conn.execute(
        "DELETE FROM orders WHERE expires IS NOT NULL AND expires < ?", (now,)).rowcount
    stale = conn.execute(
        "DELETE FROM orders WHERE seen_at < ?", (now - int(retention_hours * 3600),)).rowcount
    old_hist = conn.execute(
        "DELETE FROM history WHERE ts < ?", (now - 60 * 86400,)).rowcount
    old_snap = conn.execute(
        "DELETE FROM price_snapshots WHERE ts < ?", (now - 90 * 86400,)).rowcount
    return {"expired": expired, "stale": stale, "history": old_hist, "snapshots": old_snap}


def load_orders(conn: sqlite3.Connection, min_seen_at: int, locations=None, now: int | None = None,
                items=None):
    now = int(now if now is not None else time.time())
    sql = ("SELECT item_id, location, quality, price, amount, auction_type, seen_at "
           "FROM orders WHERE seen_at >= ? AND (expires IS NULL OR expires >= ?)")
    args: list = [min_seen_at, now]
    if locations:
        sql += f" AND location IN ({','.join('?' * len(locations))})"
        args.extend(locations)
    if items:
        items = list(items)
        sql += f" AND item_id IN ({','.join('?' * len(items))})"
        args.extend(items)
    return [dict(r) for r in conn.execute(sql, args)]


def load_daily_volumes(conn: sqlite3.Connection, index_to_item: dict, now: int | None = None) -> dict:
    """Средний объём продаж в сутки за последнюю неделю: {(item_id, location, quality): шт}.

    Используются дневные точки истории (timescale=1), а при их отсутствии —
    почасовые за последние сутки (timescale=0).
    """
    now = int(now if now is not None else time.time())
    result: dict[tuple, float] = {}
    daily = conn.execute(
        """SELECT albion_id, location, quality, SUM(item_amount) AS total,
                  COUNT(DISTINCT ts / 86400) AS days
           FROM history WHERE timescale = 1 AND ts >= ?
           GROUP BY albion_id, location, quality""",
        (now - 7 * 86400,),
    )
    for r in daily:
        item_id = index_to_item.get(str(r["albion_id"]))
        if item_id and r["days"]:
            result[(item_id, r["location"], r["quality"])] = r["total"] / max(r["days"], 1)
    hourly = conn.execute(
        """SELECT albion_id, location, quality, SUM(item_amount) AS total
           FROM history WHERE timescale = 0 AND ts >= ?
           GROUP BY albion_id, location, quality""",
        (now - 86400,),
    )
    for r in hourly:
        item_id = index_to_item.get(str(r["albion_id"]))
        key = (item_id, r["location"], r["quality"])
        if item_id and key not in result:
            result[key] = float(r["total"])
    return result
