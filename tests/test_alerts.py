import tempfile
import unittest
from pathlib import Path

from albion_trader import alerts, db

NOW = 1_800_000_000


def order(oid, price, typ="offer", loc="3008", item="T4_BAG", quality=1):
    return {"Id": oid, "ItemTypeId": item, "LocationId": loc, "QualityLevel": quality,
            "UnitPriceSilver": price * 10000, "Amount": 5, "AuctionType": typ, "Expires": "2099-01-01T00:00:00"}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "m.db"
        db.init_db(path)
        self.conn = db.connect(path)
        alerts.init(self.conn)
        self.clock = [NOW]
        self.engine = alerts.AlertEngine(lambda: self.conn, clock=lambda: self.clock[0])
        self.received = []
        self.engine.listeners.append(self.received.append)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def feed(self, *orders):
        db.ingest(self.conn, "marketorders.ingest", {"Orders": list(orders)}, now=self.clock[0])
        return self.engine.check_market(self.conn, {o["ItemTypeId"] for o in orders}, 0.04)

    def test_price_below_with_antispam(self):
        self.engine.save_rule(self.conn, {"kind": "price_below", "params": {
            "item": "T4_BAG", "locations": ["martlock"], "price": 1000, "side": "sell"}})
        self.assertEqual(self.feed(order(1, 1200)), [])
        fired = self.feed(order(2, 900))
        self.assertEqual(len(fired), 1)
        self.assertIn("900", fired[0]["text"])
        self.assertEqual(len(self.received), 1)
        # Та же цена через минуту — не повторяется; через 31 минуту — повторяется.
        self.clock[0] += 60
        self.assertEqual(self.feed(order(2, 900)), [])
        self.clock[0] += alerts.REPEAT_SECONDS
        self.assertEqual(len(self.feed(order(2, 900))), 1)

    def test_price_above_on_buy_orders_and_location_filter(self):
        self.engine.save_rule(self.conn, {"kind": "price_above", "params": {
            "item": "T4_BAG", "locations": ["thetford"], "price": 500, "side": "buy"}})
        self.assertEqual(self.feed(order(1, 800, "request")), [])            # другой город
        self.assertEqual(len(self.feed(order(2, 800, "request", loc="0007"))), 1)

    def test_disabled_rule_ignored(self):
        rid = self.engine.save_rule(self.conn, {"kind": "price_below", "enabled": False,
                                                "params": {"item": "T4_BAG", "price": 10**9}})
        self.assertEqual(self.feed(order(1, 100)), [])
        self.assertTrue(rid)

    def test_deal_rule(self):
        self.engine.save_rule(self.conn, {"kind": "deal", "params": {
            "src": ["martlock"], "dst": ["black_market"], "min_profit": 100}})
        fired = self.feed(order(1, 1000), order(2, 2000, "request", loc="3003"))
        self.assertEqual(len(fired), 1)
        self.assertIn("martlock", fired[0]["text"])  # без каталога имён — ключ рынка

    def test_item_filter_factory(self):
        self.engine.save_rule(self.conn, {"kind": "price_below", "params": {"item": "T4_BAG", "price": 10**9}})
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 5)]}, now=NOW)
        fired = self.engine.check_market(self.conn, {"T4_BAG"}, 0.04, lambda p: (lambda item: False))
        self.assertEqual(fired, [])

    def test_trigger_kind_requires_enabled_rule(self):
        self.assertEqual(self.engine.trigger_kind(self.conn, "outbid", "k", "t", "x"), [])
        self.engine.ensure_rule(self.conn, "outbid", "Перебили")
        self.engine.ensure_rule(self.conn, "outbid", "Перебили")  # второй раз не создаётся
        self.assertEqual(len(self.engine.rules(self.conn, "outbid")), 1)
        self.assertEqual(len(self.engine.trigger_kind(self.conn, "outbid", "k", "t", "x")), 1)

    def test_list_mark_seen_cleanup(self):
        self.engine.trigger(self.conn, "world_event", "a", "A", "a")
        self.engine.trigger(self.conn, "world_event", "b", "B", "b")
        items = self.engine.list_alerts(self.conn)
        self.assertEqual([a["title"] for a in items], ["B", "A"])
        self.assertEqual(len(self.engine.list_alerts(self.conn, since_id=items[1]["id"])), 1)
        self.engine.mark_seen(self.conn, [items[0]["id"]])
        self.assertEqual(sum(not a["seen"] for a in self.engine.list_alerts(self.conn)), 1)
        self.clock[0] += (alerts.KEEP_DAYS + 1) * 86400
        self.assertEqual(self.engine.cleanup(self.conn), 2)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.save_rule(self.conn, {"kind": "nope"})


class TouchedTest(EngineTest):
    def test_only_touched_markets_alert(self):
        self.engine.save_rule(self.conn, {"kind": "price_below", "params": {"item": "T4_BAG", "price": 10**6}})
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 500, loc="0007")]}, now=NOW)
        fired = self.engine.check_market(self.conn, {"T4_BAG"}, 0.04, None, {("T4_BAG", "martlock")})
        self.assertEqual(fired, [])
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(2, 600)]}, now=NOW)
        fired = self.engine.check_market(self.conn, {"T4_BAG"}, 0.04, None, {("T4_BAG", "martlock")})
        self.assertEqual([a["payload"]["location"] for a in fired], ["martlock"])


class BatchCacheTest(EngineTest):
    def test_orders_loaded_once_per_batch(self):
        from unittest import mock
        for _ in range(5):
            self.engine.save_rule(self.conn, {"kind": "deal", "params": {"min_profit": 1}})
        for item in ("T8_A", "T8_B", "T8_C"):   # предметов нет в пакете — не грузим вовсе
            self.engine.save_rule(self.conn, {"kind": "price_below", "params": {"item": item, "price": 1}})
        self.engine.save_rule(self.conn, {"kind": "price_below", "params": {"item": "T4_BAG", "price": 1}})
        # Повреждённое правило в базе (например, правка вручную) пропускается.
        self.conn.execute("INSERT INTO alert_rules (name, kind, params, created) VALUES ('x', 'deal', '\"bad\"', 0)")
        with mock.patch.object(db, "load_orders", wraps=db.load_orders) as spy:
            self.engine.check_market(self.conn, {"T4_BAG", "T5_BAG"}, 0.04)
        # Одна выборка для всех правил «сделка» и одна — для ценового правила по T4_BAG.
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(spy.call_args_list[1].kwargs["items"], frozenset({"T4_BAG"}))


class RuleParamsTest(EngineTest):
    def test_bad_param_types_rejected(self):
        for params in ({"item": "T4_BAG", "price": "x"}, {"price": float("nan")}, {"price": True},
                       {"locations": "martlock"}, {"src": [["a"]]}, {"item": 5}):
            with self.assertRaises(ValueError, msg=params):
                self.engine.save_rule(self.conn, {"kind": "price_below", "params": params})
        rid = self.engine.save_rule(self.conn, {"kind": "price_below", "params": {
            "item": "T4_BAG", "price": "1500", "locations": ["martlock"], "tiers": ["4"]}})
        rule = [r for r in self.engine.rules(self.conn) if r["id"] == rid][0]
        self.assertEqual(rule["params"]["price"], 1500.0)

    def test_broken_rule_does_not_block_others(self):
        self.conn.execute("INSERT INTO alert_rules (name, kind, params, created) VALUES "
                          "('x', 'price_below', '{\"item\": \"T4_BAG\", \"price\": \"x\"}', 0)")
        self.engine.save_rule(self.conn, {"kind": "price_below", "params": {"item": "T4_BAG", "price": 10**9}})
        db.ingest(self.conn, "marketorders.ingest", {"Orders": [order(1, 500)]}, now=NOW)
        with self.assertLogs("albion_trader.alerts", "WARNING"):
            fired = self.engine.check_market(self.conn, {"T4_BAG"}, 0.04)
        self.assertEqual(len(fired), 1)
