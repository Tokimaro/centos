import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from albion_trader import builds
from albion_trader.destiny import node_title
from albion_trader.gamedata import GameData

ITEMS = {
    "T6_2H_NATURESTAFF": {"t": 6, "slot": "mainhand", "ip": 900, "2h": 1, "spec": "COMBAT_NATURESTAFFS_GREAT"},
    "T6_MAIN_SWORD": {"t": 6, "slot": "mainhand", "ip": 900},
    "T6_OFF_SHIELD": {"t": 6, "slot": "offhand", "ip": 900},
    "T6_HEAD_CLOTH_SET1": {"t": 6, "slot": "head", "ip": 900},
    "T6_HEAD_CLOTH_SET1@1": {"t": 6, "slot": "head", "ip": 1000},
    "T6_ARMOR_CLOTH_SET1": {"t": 6, "slot": "armor", "ip": 900},
    "T6_SHOES_CLOTH_SET1": {"t": 6, "slot": "shoes", "ip": 900},
    "T6_CAPE": {"t": 6, "slot": "cape", "ip": 900},
    "T6_POTION_HEAL": {"t": 6, "slot": "potion", "ip": 900},
}
DESTINY = {"templates": {"COMBAT_SPEC": [1, 2]}, "nodes": {
    "COMBAT_NATURESTAFFS_GREAT": {"tpl": "COMBAT_SPEC", "cat": "fighting", "item": "T8_2H_NATURESTAFF", "base": False},
    "COMBAT_NATURESTAFFS": {"tpl": "COMBAT_SPEC", "cat": "fighting", "item": "T8_MAIN_NATURESTAFF", "base": True}}}


def gd():
    return GameData({"items": {k: dict(v) for k, v in ITEMS.items()}, "recipes": {}, "destiny": DESTINY})


def offer(item, loc, price, q=1):
    return {"item_id": item, "location": loc, "quality": q, "auction_type": "offer", "price": price, "seen_at": 5}


class BuildLogicTest(unittest.TestCase):
    def test_clean_build_validation(self):
        g = gd()
        ok = builds.clean_build({"name": " Лес ", "slots": {"mainhand": {"item": "T6_2H_NATURESTAFF", "quality": "9"},
                                                           "potion": {"item": "T6_POTION_HEAL", "count": 5},
                                                           "head": {"item": ""}}}, g)
        self.assertEqual(ok["name"], "Лес")
        self.assertEqual(ok["slots"]["mainhand"]["quality"], 5)
        self.assertEqual(ok["slots"]["potion"]["count"], 5)
        self.assertNotIn("head", ok["slots"])
        bad = [None, "x", {"slots": []}, {"slots": {"ring": {"item": "X"}}},
               {"slots": {"head": {"item": "T6_MAIN_SWORD"}}},                         # не тот слот
               {"slots": {"mainhand": {"item": "T6_2H_NATURESTAFF"}, "offhand": {"item": "T6_OFF_SHIELD"}}},
               {"slots": {"head": {"item": "<script>"}}}, {"slots": {"head": {"item": "T6_CAPE", "quality": "x"}}},
               {"slots": {"head": "T6_HEAD"}}]
        for b in bad:
            with self.assertRaises(ValueError, msg=b):
                builds.clean_build(b, g)
        self.assertEqual(builds.clean_build({"slots": {}}, g)["name"], "Без названия")

    def test_item_power(self):
        g = gd()
        self.assertEqual(builds.item_power(g, "T6_HEAD_CLOTH_SET1@1", 5), 1100)
        self.assertIsNone(builds.item_power(g, "UNKNOWN", 1))
        two = {"mainhand": {"item": "T6_2H_NATURESTAFF", "quality": 1}, "head": {"item": "T6_HEAD_CLOTH_SET1@1", "quality": 2}}
        # (900 оружие + 900 вторая рука (двуручное) + 1020 + 0 + 0 + 0) / 6
        self.assertAlmostEqual(builds.average_ip(g, two), (900 * 2 + 1020) / 6)
        one = {"mainhand": {"item": "T6_MAIN_SWORD", "quality": 1}}
        self.assertAlmostEqual(builds.average_ip(g, one), 900 / 6)
        self.assertIsNone(builds.average_ip(g, {}))

    def test_price_quality_not_below_and_totals(self):
        g = gd()
        b = builds.clean_build({"slots": {"mainhand": {"item": "T6_2H_NATURESTAFF", "quality": 2},
                                          "potion": {"item": "T6_POTION_HEAL", "count": 10}}}, g)
        orders = [offer("T6_2H_NATURESTAFF", "martlock", 100, q=1),      # качество ниже — не подходит
                  offer("T6_2H_NATURESTAFF", "martlock", 200, q=3),      # выше — подходит
                  offer("T6_2H_NATURESTAFF", "lymhurst", 150, q=2),
                  offer("T6_POTION_HEAL", "martlock", 10),
                  {"item_id": "T6_POTION_HEAL", "location": "lymhurst", "quality": 1, "auction_type": "request",
                   "price": 1, "seen_at": 5}]
        res = builds.price_build(g, b, orders, ["martlock", "lymhurst"])
        staff, potion = res["rows"]
        self.assertEqual((staff["best_market"], staff["best_price"]), ("lymhurst", 150))
        self.assertEqual(staff["markets"]["martlock"]["price"], 200)
        self.assertEqual((potion["best_price"], potion["best_market"]), (100, "martlock"))
        self.assertEqual(res["cheapest_total"], 250)
        self.assertTrue(res["complete"])
        by = {m["market"]: m for m in res["markets"]}
        self.assertEqual((by["martlock"]["total"], by["martlock"]["complete"]), (300, True))
        self.assertEqual(by["lymhurst"]["missing"], ["potion"])      # запросы на покупку не считаются
        self.assertEqual(res["markets"][0]["market"], "martlock")     # полный набор — выше

    def test_missing_everywhere(self):
        g = gd()
        b = builds.clean_build({"slots": {"cape": {"item": "T6_CAPE"}}}, g)
        res = builds.price_build(g, b, [], ["martlock"])
        self.assertFalse(res["complete"])
        self.assertIsNone(res["rows"][0]["best_price"])

    def test_node_title(self):
        g = gd()
        names = {"T8_2H_NATURESTAFF": "Большой природный посох (старейшина)", "T8_MAIN_NATURESTAFF": "Природный посох (старейшина)"}
        name_of = lambda i: names.get(i, i)
        self.assertEqual(node_title(g, name_of, "COMBAT_NATURESTAFFS_GREAT"), "Бой: Большой природный посох")
        self.assertEqual(node_title(g, name_of, "COMBAT_NATURESTAFFS"), "Бой: Природный посох (ветка)")
        self.assertEqual(node_title(g, name_of, "NOPE"), "NOPE")


class BuildApiTest(unittest.TestCase):
    def setUp(self):
        from albion_trader.server import App, AppConfig, make_handler
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "gamedata.json").write_text(json.dumps({"version": 2, "items": ITEMS, "recipes": {}, "destiny": DESTINY}))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", capture=False))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.app.ingest("marketorders.ingest", {"Orders": [
            {"Id": 1, "ItemTypeId": "T6_CAPE", "LocationId": "3008", "QualityLevel": 1, "EnchantmentLevel": 0,
             "UnitPriceSilver": 40000 * 10000, "Amount": 1, "AuctionType": "offer", "Expires": "2099-01-01T00:00:00"}]})

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_save_list_delete(self):
        build = {"name": "Посох", "slots": {"mainhand": {"item": "T6_2H_NATURESTAFF"}, "cape": {"item": "T6_CAPE"}}}
        res = self.post("/api/builds", {"action": "save", "build": build})
        bid = res["id"]
        self.assertEqual([b["name"] for b in res["builds"]], ["Посох"])
        build["name"] = "Посох 2"
        res = self.post("/api/builds", {"action": "save", "build": build, "id": bid})
        self.assertEqual((res["id"], [b["name"] for b in res["builds"]]), (bid, ["Посох 2"]))
        with urllib.request.urlopen(self.base + "/api/builds") as r:
            data = json.load(r)
        self.assertEqual(data["builds"][0]["slots"]["cape"]["item"], "T6_CAPE")
        self.assertEqual(data["slots"][0], ["mainhand", "Оружие"])
        res = self.post("/api/builds", {"action": "delete", "id": bid})
        self.assertEqual(res["builds"], [])

    def test_price_endpoint(self):
        res = self.post("/api/build-price", {"build": {"slots": {"cape": {"item": "T6_CAPE"},
                                                                 "mainhand": {"item": "T6_2H_NATURESTAFF"}}},
                                             "markets": ["martlock", "black_market", 5]})
        self.assertEqual([m["market"] for m in res["markets"]], ["martlock"])
        cape = [r for r in res["rows"] if r["slot"] == "cape"][0]
        self.assertEqual((cape["best_price"], cape["best_market"]), (40000, "martlock"))
        staff = [r for r in res["rows"] if r["slot"] == "mainhand"][0]
        self.assertTrue(staff["spec_name"].startswith("Бой:"))
        self.assertAlmostEqual(res["average_ip"], (900 * 2 + 900) / 6)

    def test_default_markets_not_duplicated(self):
        res = self.post("/api/build-price", {"build": {"slots": {"cape": {"item": "T6_CAPE"}}}})
        names = [m["market"] for m in res["markets"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("caerleon", names)
        res = self.post("/api/build-price", {"build": {"slots": {"cape": {"item": "T6_CAPE"}}},
                                             "markets": ["martlock", "martlock"]})
        self.assertEqual([(m["market"], m["total"]) for m in res["markets"]], [("martlock", 40000)])

    def test_bad_input_400(self):
        for path, body in (("/api/builds", {"action": "save", "build": {"slots": {"ring": {}}}}),
                           ("/api/builds", {"action": "delete", "id": "x"}),
                           ("/api/builds", {"action": "bogus"}),
                           ("/api/build-price", {"build": "x"})):
            with self.assertRaises(urllib.error.HTTPError, msg=body) as cm:
                self.post(path, body)
            self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
