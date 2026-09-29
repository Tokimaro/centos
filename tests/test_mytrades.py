import json
import struct
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from albion_trader import db, mytrades
from albion_trader.capture import photon
from albion_trader.capture.albion import AlbionState
from albion_trader.server import App, AppConfig

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb

NOW = 1_800_000_000
TICKS = lambda ts: ts * 10_000_000 + 621_355_968_000_000_000


def my_order(oid, price, typ="offer", item="T4_BAG", amount=3):
    return {"Id": oid, "ItemTypeId": item, "LocationId": "3008", "QualityLevel": 2,
            "UnitPriceSilver": price * 10000, "Amount": amount, "AuctionType": typ, "Expires": "2099-01-01T00:00:00"}


class MyTradesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "m.db"
        db.init_db(self.path)
        with self.conn() as c:
            mytrades.init(c)
        self.clock = [NOW]
        self.mt = mytrades.MyTrades(self.conn, threading.Lock(), clock=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def conn(self):
        c = db.connect(self.path)
        try:
            with c:
                yield c
        finally:
            c.close()

    def query(self, sql, *args):
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def test_open_orders_replaced_by_full_list(self):
        self.mt.handle_my_orders("offers", [my_order(1, 100), my_order(2, 200)])
        self.clock[0] += 10
        self.mt.handle_my_orders("offers", [my_order(2, 190)])
        rows = {r["id"]: r for r in self.query("SELECT * FROM my_orders")}
        self.assertEqual((rows[1]["active"], rows[2]["active"], rows[2]["price"]), (0, 1, 190))
        self.assertEqual(rows[2]["location"], "martlock")
        # Список запросов не трогает предложения.
        self.mt.handle_my_orders("requests", [my_order(3, 50, "request")])
        self.assertEqual(len(self.query("SELECT * FROM my_orders WHERE active = 1")), 2)

    def test_finished_auctions_become_trades_once(self):
        self.mt.handle_my_orders("finished", [my_order(5, 300, amount=2)])
        self.mt.handle_my_orders("finished", [my_order(5, 300, amount=2)])
        t = self.query("SELECT * FROM my_trades")
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["kind"], t[0]["amount"], t[0]["unit_price"], t[0]["total"]), ("sell", 2, 300, 600))

    def test_instant_buy_and_sell_use_known_order_price(self):
        with self.conn() as c:
            db.ingest(c, "marketorders.ingest", {"Orders": [
                {"Id": 77, "ItemTypeId": "T5_BAG", "LocationId": "0007", "QualityLevel": 1,
                 "UnitPriceSilver": 5000 * 10000, "Amount": 9, "AuctionType": "offer", "Expires": "2099-01-01T00:00:00"},
                {"Id": 78, "ItemTypeId": "T5_BAG", "LocationId": "3003", "QualityLevel": 1,
                 "UnitPriceSilver": 8000 * 10000, "Amount": 9, "AuctionType": "request", "Expires": "2099-01-01T00:00:00"}]},
                now=NOW)
        self.mt.handle_buy_offer({0: 1, 1: 2, 2: 77})
        self.mt.handle_sell_to_request({0: 1, 1: 78, 2: 999, 4: 2})
        self.mt.handle_buy_offer({1: 1, 2: 123456})  # неизвестный заказ — пропускается
        t = {r["kind"]: r for r in self.query("SELECT * FROM my_trades")}
        self.assertEqual((t["buy"]["location"], t["buy"]["total"]), ("thetford", 10000))
        self.assertEqual((t["sell"]["location"], t["sell"]["unit_price"]), ("black_market", 8000))

    def test_mail_infos_both_layouts_and_read(self):
        types = ["MARKETPLACE_SELLORDER_FINISHED_SUMMARY", "MARKETPLACE_BUYORDER_FINISHED_SUMMARY", "OTHER"]
        # Раскладка albiondata-client: 3 id, 6 место, 10 тип, 11 время.
        self.mt.handle_mail_infos({3: [11, 12, 13], 6: ["3008", "0007", "x"], 10: types,
                                   11: [TICKS(NOW - 60)] * 3})
        # Раскладка StatisticsAnalysisTool: 3 id, 7 тема, 11 тип, 12 время.
        self.mt.handle_mail_infos({0: "guid", 3: [21], 7: ["subj"], 11: types[:1], 12: [TICKS(NOW - 30)]})
        self.mt.handle_read_mail({0: 11, 1: f"4|T4_BAG|{4 * 250 * 10000}|{250 * 10000}"})
        self.mt.handle_read_mail({0: 12, 1: f"2|T5_BAG|{2 * 900 * 10000}|{900 * 10000}"})
        self.mt.handle_read_mail({0: 13, 1: "1|X|1|1"})       # не рыночное письмо
        self.mt.handle_read_mail({0: 21, 1: f"1|T6_BAG|{700 * 10000}|{700 * 10000}"})
        self.mt.handle_read_mail({0: 11, 1: f"4|T4_BAG|{4 * 250 * 10000}|{250 * 10000}"})  # повтор
        t = {r["item_id"]: r for r in self.query("SELECT * FROM my_trades")}
        self.assertEqual(sorted(t), ["T4_BAG", "T5_BAG", "T6_BAG"])
        self.assertEqual((t["T4_BAG"]["kind"], t["T4_BAG"]["amount"], t["T4_BAG"]["unit_price"],
                          t["T4_BAG"]["location"], t["T4_BAG"]["ts"]), ("sell", 4, 250, "martlock", NOW - 60))
        self.assertEqual(t["T5_BAG"]["kind"], "buy")
        self.assertEqual(t["T6_BAG"]["ts"], NOW - 30)

    def test_summary_profit_by_average_cost(self):
        rows = [
            {"ts": NOW, "kind": "buy", "item_id": "A", "amount": 2, "total": 200},
            {"ts": NOW + 1, "kind": "buy", "item_id": "A", "amount": 2, "total": 400},
            {"ts": NOW + 2, "kind": "sell", "item_id": "A", "amount": 3, "total": 600},
            {"ts": NOW + 3, "kind": "sell", "item_id": "B", "amount": 1, "total": 100},
        ]
        rep = mytrades.summary(rows, tax=0.04)
        a = {i["item_id"]: i for i in rep["items"]}["A"]
        self.assertEqual((a["avg_cost"], a["stock"]), (150, 1))
        self.assertAlmostEqual(a["profit"], 600 * 0.96 - 150 * 3)
        b = {i["item_id"]: i for i in rep["items"]}["B"]
        self.assertIsNone(b["profit"])     # себестоимость неизвестна
        self.assertAlmostEqual(rep["days"][0]["profit"], 600 * 0.96 - 450)


class CaptureToMyOrdersTest(unittest.TestCase):
    def test_my_offers_response_lands_in_my_orders(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            parser = photon.PhotonParser(app.albion.on_request, app.albion.on_response, app.albion.on_event)
            app.albion.location = "3008"
            body = bytes([92]) + struct.pack("<h", 0) + pb.value([json.dumps(my_order(9, 123))]) + pb.params({})
            parser.receive_packet(pb.packet(pb.command(3, body)))
            data = app.api_my_orders({})
            self.assertEqual([r["id"] for r in data["rows"]], [9])
            self.assertEqual(app.api_status({})["total_orders"], 0)   # в рыночный стакан не попал
