import tempfile
import unittest
from pathlib import Path

from albion_trader import db
from albion_trader.locations import normalize_location

NOW = 1_800_000_000


def order(oid, price, typ="offer", loc="3008", item="T4_BAG", quality=1, amount=1,
          expires="2030-01-01T00:00:00.1234567"):
    return {"Id": oid, "ItemTypeId": item, "ItemGroupTypeId": item, "LocationId": loc,
            "QualityLevel": quality, "EnchantmentLevel": 0, "UnitPriceSilver": price * 10000,
            "Amount": amount, "AuctionType": typ, "Expires": expires}


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "m.db"
        db.init_db(path)
        self.conn = db.connect(path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def prices(self, typ="offer"):
        return sorted(r[0] for r in self.conn.execute(
            "SELECT price FROM orders WHERE auction_type=?", (typ,)))

    def test_ingest_and_upsert(self):
        n = db.ingest(self.conn, "marketorders.ingest",
                      {"Orders": [order(1, 100), order(2, 200, "request", loc="3003")]}, now=NOW)
        self.assertEqual(n, 2)
        rows = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM orders")}
        self.assertEqual(rows[1]["location"], "martlock")
        self.assertEqual(rows[2]["location"], "black_market")
        self.assertEqual(rows[1]["price"], 100)
        self.assertEqual(rows[1]["expires"], 1893456000)
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 90)]}, now=NOW + 10)
        self.assertEqual(self.prices(), [90])

    def test_invalid_orders_skipped(self):
        bad = [{"Id": 1}, order(2, 0), order(3, 10, typ="weird"), "junk"]
        self.assertEqual(db.ingest(self.conn, "marketorders.ingest", {"Orders": bad}, now=NOW), 0)

    def test_prune_superseded_offers_after_grace(self):
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 100), order(2, 150)]}, now=NOW)
        # Сразу же пришла «вторая страница» — первая не должна пропасть.
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(3, 300)]}, now=NOW + 30)
        self.assertEqual(self.prices(), [100, 150, 300])
        # Через час свежий снимок начинается со 150 — заказ за 100 уже выкуплен.
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(2, 150)]}, now=NOW + 3600)
        self.assertEqual(self.prices(), [150, 300])

    def test_prune_superseded_requests(self):
        db.ingest(self.conn, "marketorders.ingest",
                  {"Orders": [order(1, 500, "request"), order(2, 400, "request")]}, now=NOW)
        db.ingest(self.conn, "marketorders.ingest",
                  {"Orders": [order(2, 400, "request")]}, now=NOW + 3600)
        self.assertEqual(self.prices("request"), [400])

    def test_cleanup(self):
        db.ingest(self.conn, "marketorders.ingest",
                  {"Orders": [order(1, 100, expires="2020-01-01T00:00:00"), order(2, 100, item="A")]},
                  now=NOW)
        res = db.cleanup(self.conn, retention_hours=1, now=NOW + 7200)
        self.assertEqual(res["expired"], 1)
        self.assertEqual(res["stale"], 1)

    def test_history_and_volumes(self):
        day = 86400
        ticks = lambda ts: ts * 10_000_000 + 621_355_968_000_000_000
        payload = {"AlbionId": 42, "LocationId": "3003", "QualityLevel": 1, "Timescale": 1,
                   "MarketHistories": [
                       {"ItemAmount": 10, "SilverAmount": 1, "Timestamp": ticks(NOW - day)},
                       {"ItemAmount": 30, "SilverAmount": 1, "Timestamp": ticks(NOW - 2 * day)}]}
        self.assertEqual(db.ingest(self.conn, "markethistories.ingest", payload, now=NOW), 2)
        vols = db.load_daily_volumes(self.conn, {"42": "T4_BAG"}, now=NOW)
        self.assertEqual(vols[("T4_BAG", "black_market", 1)], 20)

    def test_unknown_topic_counted(self):
        db.ingest(self.conn, "mapdata.ingest", {"x": 1}, now=NOW)
        row = self.conn.execute("SELECT * FROM ingest_stats WHERE topic='mapdata.ingest'").fetchone()
        self.assertEqual(row["batches"], 1)


class LocationsTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_location("3005"), "caerleon")
        self.assertEqual(normalize_location("3013-Auction2"), "caerleon")
        self.assertEqual(normalize_location(7), "thetford")
        self.assertEqual(normalize_location("3003"), "black_market")
        self.assertEqual(normalize_location("5003"), "brecilien")
        self.assertEqual(normalize_location("1301"), "lymhurst")
        self.assertEqual(normalize_location("0301"), "thetford")
        self.assertEqual(normalize_location(3013), "caerleon")
        self.assertEqual(normalize_location("BLACKBANK-2310"), "BLACKBANK-2310")
        self.assertIsNone(normalize_location(""))


if __name__ == "__main__":
    unittest.main()


class MigrationTest(unittest.TestCase):
    def test_portal_rows_moved_to_city(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            db.init_db(path)
            conn = db.connect(path)
            with conn:
                conn.execute("INSERT INTO orders(id, item_id, location, quality, enchant, price, amount,"
                             " auction_type, seen_at) VALUES (1, 'X', 'lymhurst_portal', 1, 0, 5, 1, 'offer', 1)")
            conn.close()
            db.init_db(path)
            conn = db.connect(path)
            self.assertEqual(conn.execute("SELECT location FROM orders").fetchone()[0], "lymhurst")
            conn.close()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "m.db"
        db.init_db(path)
        self.conn = db.connect(path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def snaps(self):
        return db.load_snapshots(self.conn, "T4_BAG", 0)

    def test_snapshot_written_on_change_or_interval(self):
        db.ingest(self.conn, "marketorders.ingest",
                  {"Orders": [order(1, 100), order(2, 80, "request")]}, now=NOW)
        s = self.snaps()
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["sell_min"], s[0]["buy_max"], s[0]["location"]), (100, 80, "martlock"))
        # Та же цена через минуту — нового снимка нет.
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 100)]}, now=NOW + 60)
        self.assertEqual(len(self.snaps()), 1)
        # Цена изменилась — снимок есть.
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(3, 95)]}, now=NOW + 120)
        self.assertEqual(self.snaps()[-1]["sell_min"], 95)
        # Та же цена, но прошло больше интервала — снимок есть.
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(3, 95)]}, now=NOW + 120 + db.SNAPSHOT_INTERVAL)
        self.assertEqual(len(self.snaps()), 3)

    def test_sales_prefer_daily_points(self):
        ticks = lambda ts: ts * 10_000_000 + 621_355_968_000_000_000
        for timescale, amount in ((0, 5), (1, 10)):
            db.ingest(self.conn, "markethistories.ingest", {
                "AlbionId": 7, "LocationId": "3008", "QualityLevel": 1, "Timescale": timescale,
                "MarketHistories": [{"ItemAmount": amount, "SilverAmount": amount * 250 * 10000,
                                     "Timestamp": ticks(NOW - 3600)}]}, now=NOW)
        sales = db.load_sales(self.conn, 7, 0)
        self.assertEqual(len(sales), 1)
        self.assertEqual((sales[0]["amount"], sales[0]["avg_price"]), (10, 250))

    def test_cleanup_old_snapshots(self):
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 100)]}, now=NOW)
        res = db.cleanup(self.conn, retention_hours=10**6, now=NOW + 91 * 86400)
        self.assertEqual(res["snapshots"], 1)


class ReferencePriceTest(SnapshotTest):
    def test_median_needs_three_snapshots_else_sales(self):
        for i, price in enumerate((100, 300, 200)):
            db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(10 + i, price)]},
                      now=NOW + i * (db.SNAPSHOT_INTERVAL + db.PRUNE_GRACE_SECONDS + 1))
        refs = db.reference_prices(self.conn, 0, {"7": "T4_BAG", "8": "T5_BAG"})
        # Снимки: 100; затем свежий стакан начинается с 300 — заказ за 100 считается выкупленным;
        # затем лучший 200. Медиана (100, 300, 200) = 200.
        self.assertEqual(refs[("T4_BAG", "martlock", 1)], (200, "median", 3))
        ticks = lambda ts: ts * 10_000_000 + 621_355_968_000_000_000
        db.ingest(self.conn, "markethistories.ingest", {
            "AlbionId": 8, "LocationId": "3008", "QualityLevel": 1, "Timescale": 1,
            "MarketHistories": [{"ItemAmount": 4, "SilverAmount": 4 * 500 * 10000, "Timestamp": ticks(NOW)}]}, now=NOW)
        refs = db.reference_prices(self.conn, 0, {"7": "T4_BAG", "8": "T5_BAG"})
        self.assertEqual(refs[("T5_BAG", "martlock", 1)], (500, "sales", 1))
