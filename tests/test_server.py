import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from albion_trader.server import App, AppConfig, make_handler


class ServerTest(unittest.TestCase):
    token = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", token=self.token))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def orders(self):
        mk = lambda i, loc, typ, price: {
            "Id": i, "ItemTypeId": "T5_BAG", "LocationId": loc, "QualityLevel": 1,
            "EnchantmentLevel": 0, "UnitPriceSilver": price * 10000, "Amount": 2,
            "AuctionType": typ, "Expires": "2099-01-01T00:00:00"}
        return {"Orders": [mk(1, "0007", "offer", 1000), mk(2, "3003", "request", 5000)]}


class OpenServerTest(ServerTest):
    def test_ingest_then_deals(self):
        status, body = self.post("/marketorders.ingest", self.orders())
        self.assertEqual((status, body["saved"]), (200, 2))
        deals = self.get("/api/deals?max_age=1")
        self.assertEqual(deals["count"], 1)
        d = deals["deals"][0]
        self.assertEqual((d["source"], d["destination"]), ("thetford", "black_market"))
        self.assertEqual(d["tier"], 5)
        prices = self.get("/api/prices?item=T5_BAG")
        self.assertEqual(len(prices["rows"]), 2)
        status_info = self.get("/api/status")
        self.assertEqual(status_info["total_orders"], 2)
        self.assertEqual(self.get("/api/items?q=t5")["items"][0]["item_id"], "T5_BAG")
        recent = self.get("/api/items?recent=1")["items"][0]
        self.assertEqual((recent["item_id"], recent["markets"], recent["orders"]), ("T5_BAG", 2, 2))

    def test_static_and_404(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Albion Trader", r.read())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/../server.py")
        self.assertEqual(cm.exception.code, 404)

    def test_other_topics_accepted(self):
        status, body = self.post("/goldprices.ingest", {"Prices": [5000], "Timestamps": [1800000000]})
        self.assertEqual((status, body["saved"]), (200, 1))


class TokenServerTest(ServerTest):
    token = "s3cret"

    def test_token_required(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/marketorders.ingest", self.orders())
        self.assertEqual(cm.exception.code, 403)
        status, body = self.post("/s3cret/marketorders.ingest", self.orders())
        self.assertEqual(body["saved"], 2)
        self.assertEqual(self.get("/api/status")["ingest_path"], "/s3cret")


if __name__ == "__main__":
    unittest.main()


class FastSellApiTest(ServerTest):
    def test_fastsell_endpoint(self):
        self.post("/marketorders.ingest", self.orders())
        mk = {"Id": 9, "ItemTypeId": "T5_BAG", "LocationId": "0007", "QualityLevel": 1,
              "UnitPriceSilver": 3000 * 10000, "Amount": 1, "AuctionType": "request",
              "Expires": "2099-01-01T00:00:00"}
        self.post("/marketorders.ingest", {"Orders": [mk]})
        data = self.get("/api/fastsell?base=thetford&locs=thetford,black_market")
        self.assertEqual(data["count"], 1)
        row = data["rows"][0]
        self.assertEqual(row["best_location"], "black_market")
        self.assertAlmostEqual(row["gain_vs_base"], (5000 - 3000) * 0.96)


class SettingsApiTest(ServerTest):
    def post_json(self, path, body, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_defaults_and_update(self):
        s = self.get("/api/settings")
        self.assertTrue(s["premium"])
        s = self.post_json("/api/settings", {"premium": False, "station_fee": 250})
        self.assertEqual((s["premium"], s["station_fee"]), (False, 250))
        self.assertEqual(self.get("/api/settings")["station_fee"], 250)

    def test_rejects_cross_origin_and_non_json(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post_json("/api/settings", {"premium": False}, {"Origin": "http://evil.example"})
        self.assertEqual(cm.exception.code, 403)
        req = urllib.request.Request(self.base + "/api/settings", data=b"premium=0",
                                     headers={"Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 415)
        self.assertTrue(self.get("/api/settings")["premium"])


class FlipsApiTest(ServerTest):
    def test_flips_endpoint(self):
        mk = lambda i, typ, price: {"Id": i, "ItemTypeId": "T4_BAG", "LocationId": "3008", "QualityLevel": 1,
                                   "UnitPriceSilver": price * 10000, "Amount": 5, "AuctionType": typ,
                                   "Expires": "2099-01-01T00:00:00"}
        self.post("/marketorders.ingest", {"Orders": [mk(1, "request", 1000), mk(2, "offer", 2000)]})
        data = self.get("/api/flips?min_margin=0")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rows"][0]["location"], "martlock")
        self.assertEqual(self.get("/api/flips?min_margin=500")["count"], 0)


class AlertsApiTest(SettingsApiTest):
    def test_rule_lifecycle_and_alert_on_ingest(self):
        res = self.post_json("/api/alert-rules", {"action": "save", "rule": {
            "kind": "price_below", "name": "Дешёвая сумка", "params": {"item": "T5_BAG", "price": 1500}}})
        rid = res["id"]
        self.assertEqual(res["rules"][0]["name"], "Дешёвая сумка")
        self.assertIn("price_below", res["kinds"])
        self.post("/marketorders.ingest", self.orders())   # предложение T5_BAG за 1000 в Тетфорде
        data = self.get("/api/alerts")
        self.assertEqual((len(data["alerts"]), data["unseen"]), (1, 1))
        self.assertIn("Тетфорд", data["alerts"][0]["text"])
        self.post_json("/api/alerts/seen", {})
        self.assertEqual(self.get("/api/alerts")["unseen"], 0)
        self.post_json("/api/alert-rules", {"action": "toggle", "id": rid, "enabled": False})
        self.assertFalse(self.get("/api/alert-rules")["rules"][0]["enabled"])
        self.post_json("/api/alert-rules", {"action": "delete", "id": rid})
        self.assertEqual(self.get("/api/alert-rules")["rules"], [])

    def test_bad_rule_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post_json("/api/alert-rules", {"action": "save", "rule": {"kind": "bogus"}})
        self.assertEqual(cm.exception.code, 400)
