"""Регрессионные тесты на ошибки, найденные при ревью и фаззинге."""
import http.client
import struct
import unittest
import urllib.error
import urllib.parse

from albion_trader import alerts as alerts_mod
from albion_trader.capture import photon
from albion_trader.server import _float, _limit, _split, LIST_LIMIT, PARAM_LIMIT

try:
    from . import photon_builder as pb
    from .test_server import SettingsApiTest
except ImportError:  # запуск через discover -s tests
    import photon_builder as pb
    from test_server import SettingsApiTest

GET_ENDPOINTS = ["/api/deals", "/api/prices", "/api/fastsell", "/api/flips", "/api/history",
                 "/api/underpriced", "/api/bm-demand", "/api/craft", "/api/enchant", "/api/journals",
                 "/api/farming", "/api/alerts", "/api/my/orders", "/api/gold", "/api/session",
                 "/api/loot", "/api/character", "/api/kills", "/api/zones", "/api/world",
                 "/api/my/trades", "/api/items", "/api/chain", "/api/builds", "/api/killboard", "/api/destiny",
                 "/api/avalon", "/api/dungeons", "/api/economy", "/api/window"]
PARAMS = ["max_age", "min_profit", "min_margin", "limit", "hours", "days", "budget", "tax",
          "price", "since", "quantity", "item", "items", "cities", "q", "qty", "quality", "min_ip", "period",
          "session", "station_fee", "from", "to"]


class ParamHelpersTest(unittest.TestCase):
    def test_float_rejects_nan_inf_and_clamps(self):
        for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "abc", None, ""):
            self.assertEqual(_float(bad, 7), 7, bad)
        self.assertEqual(_float("1e400", 0), 0)       # переполнение → inf → по умолчанию
        self.assertEqual(_float("1e300", 0), PARAM_LIMIT)
        self.assertEqual(_float("-1e300", 0), -PARAM_LIMIT)
        self.assertEqual(_float("2.5", 0), 2.5)

    def test_limit_never_negative(self):
        self.assertEqual(_limit({"limit": "-5"}, 10), 0)
        self.assertEqual(_limit({"limit": "nan"}, 10), 10)
        self.assertEqual(_limit({}, 10), 10)

    def test_split_capped(self):
        self.assertEqual(len(_split(",".join(str(i) for i in range(1000)))), LIST_LIMIT)
        self.assertEqual(_split("a,,b"), ["a", "b"])
        self.assertEqual(_split(None), [])


class HostileGetTest(SettingsApiTest):
    def test_bad_numbers_never_500(self):
        self.post("/marketorders.ingest", self.orders())
        for path in GET_ENDPOINTS:
            for value in ("nan", "inf", "-inf", "1e400", "-1", "99999999999999999999999", "x"):
                qs = urllib.parse.urlencode({p: value for p in PARAMS})
                try:
                    self.get(f"{path}?{qs}")
                except urllib.error.HTTPError as e:
                    self.assertLess(e.code, 500, f"{path}?{value}")

    def test_foreign_host_header_rejected(self):
        port = self.httpd.server_address[1]
        for host, expected in (("evil.example", 403), ("evil.example:%d" % port, 403),
                               ("127.0.0.1:%d" % port, 200), ("localhost", 200), ("[::1]:80", 200)):
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/api/status", headers={"Host": host})
            self.assertEqual(conn.getresponse().status, expected, host)
            conn.close()


class HostileIngestTest(SettingsApiTest):
    def test_bad_orders_skipped_good_kept(self):
        good = self.orders()["Orders"]
        bad = [dict(good[0], Id=2 ** 70), dict(good[0], Id=-(2 ** 70)), dict(good[0], Id=True),
               dict(good[0], UnitPriceSilver=2 ** 80), dict(good[0], Amount=[1]),
               dict(good[0], ItemTypeId={"a": 1}), dict(good[0], QualityLevel=10 ** 30),
               dict(good[0], UnitPriceSilver=0), dict(good[0], LocationId=[1]), "junk", None]
        status, body = self.post("/marketorders.ingest", {"Orders": bad + good})
        self.assertEqual((status, body["saved"]), (200, 2))

    def test_bad_history_and_gold_do_not_500(self):
        for path, body in (("/markethistories.ingest", {"AlbionId": 2 ** 80, "MarketHistories": [
                               {"ItemAmount": "x", "SilverAmount": 2 ** 90, "Timestamp": -1}]}),
                           ("/goldprices.ingest", {"Prices": [2 ** 90, "x"], "Timestamps": [1, 2]}),
                           ("/marketorders.ingest", {"Orders": "nope"}),
                           ("/marketorders.ingest", [1, 2, 3])):
            try:
                status, _ = self.post(path, body)
                self.assertEqual(status, 200)
            except urllib.error.HTTPError as e:
                self.assertLess(e.code, 500, path)

    def test_bad_alert_payloads_are_400(self):
        payloads = [{"action": "toggle", "id": "abc"}, {"action": "delete", "id": 2 ** 80},
                    {"action": "toggle", "id": [1]}, {"action": "save", "rule": "str"},
                    {"action": "save", "rule": {"kind": "price_below", "params": "str"}}]
        for p in payloads:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.post_json("/api/alert-rules", p)
            self.assertEqual(cm.exception.code, 400, p)
        # Некорректные id в seen просто игнорируются.
        self.post_json("/api/alerts/seen", {"ids": ["x", None, 2 ** 80, 1]})


class AlertEngineTest(unittest.TestCase):
    def test_rule_id_validation(self):
        self.assertEqual(alerts_mod._rule_id("5"), 5)
        for bad in ("x", None, [1], 2 ** 80, True, 1.5):
            with self.assertRaises((ValueError, TypeError)):
                alerts_mod._rule_id(bad)


class FragmentBoundsTest(unittest.TestCase):
    def test_out_of_bounds_fragment_ignored(self):
        seen = []
        p = photon.PhotonParser(on_request=lambda c, prm: seen.append(prm[1]))
        data = pb.command(2, bytes([1]) + pb.params({1: "z" * 200}))[12:]

        def send(num, off, chunk):
            frag = struct.pack(">IIIII", 7, 2, num, len(data), off) + chunk
            p.receive_packet(pb.packet(bytes([8, 0, 0, 0]) + struct.pack(">II", 12 + len(frag), num) + frag))
        # Кусок, вылезающий за пределы сообщения, не должен ни засчитываться,
        # ни раздувать буфер.
        send(5, len(data) - 3, b"\xff" * 50)
        self.assertEqual(len(p.pending[7]["buf"]), len(data))
        self.assertEqual(p.pending[7]["written"], 0)
        half = len(data) // 2
        send(0, 0, data[:half])
        send(1, half, data[half:])
        self.assertEqual(seen, ["z" * 200])


if __name__ == "__main__":
    unittest.main()


class NonFiniteEventTest(unittest.TestCase):
    def test_inf_and_nan_in_events_ignored(self):
        import math
        import tempfile
        from pathlib import Path
        from albion_trader import activity
        from albion_trader.server import App, AppConfig
        self.assertIsNone(activity._fix(float("inf")))
        self.assertIsNone(activity._fix(float("nan")))
        self.assertIsNone(activity._fix(10 ** 30))
        self.assertIsNone(activity._int(float("inf")))
        self.assertIsNone(activity._int(True))
        self.assertEqual(activity._int("12"), 12)
        from albion_trader import world
        self.assertIsNone(world._ts(float("inf")))
        self.assertIsNone(world._ts(-5))
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            p = photon.PhotonParser(app.albion.on_request, app.albion.on_response, app.albion.on_event)
            inf = float("inf")
            for code, prm in ((62, {0: inf, 3: inf, 4: inf}), (82, {2: inf, 10: inf, 17: inf}),
                              (393, {0: inf, 3: "C", 21: inf}), (395, {0: inf}), (279, {2: "x", 3: True, 5: inf}),
                              (61, {0: inf}), (480, {0: inf, 1: inf, 2: inf, 3: inf, 4: inf})):
                p.receive_packet(pb.packet(pb.event(code, prm)))
            with app.conn() as c:
                values = [r[0] for r in c.execute("SELECT value FROM activity_events WHERE value IS NOT NULL")]
            self.assertTrue(all(math.isfinite(v) for v in values))
            app.api_zones({})
            app.api_economy({"period": "7"})
