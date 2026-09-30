import unittest

from albion_trader.chain import ChainParams, build_chain, items_in_chain
from albion_trader.gamedata import GameData
from albion_trader.production import PriceBook, return_rate


def gd():
    return GameData({
        "items": {"T4_BAG": {"t": 4, "cc": "bag", "v": 0}, "T4_LEATHER": {"t": 4, "cc": "leather", "v": 0, "sub": "refinedresources"},
                  "T4_CLOTH": {"t": 4, "cc": "cloth", "v": 0}, "T3_LEATHER": {"t": 3, "cc": "leather", "v": 0, "sub": "refinedresources"},
                  "T4_HIDE": {"t": 4}},
        "recipes": {
            "T4_BAG": {"res": [["T4_LEATHER", 8, True], ["T4_CLOTH", 8, True]], "n": 1, "silver": 0,
                       "focus": 500, "kind": "craft"},
            "T4_LEATHER": {"res": [["T4_HIDE", 2, True], ["T3_LEATHER", 1, True]], "n": 1, "silver": 0,
                           "focus": 50, "kind": "refine"},
            # цикл: T3_LEATHER «делается» из T4_LEATHER — не должен зациклить расчёт
            "T3_LEATHER": {"res": [["T4_LEATHER", 1, True]], "n": 1, "silver": 0, "kind": "refine"},
        },
        "cities": {"martlock": {"craft": 0.18, "refine": 0.18, "mods": {"leather": 0.4}}},
    })


def offer(item, price, loc="martlock", q=1):
    return {"item_id": item, "location": loc, "quality": q, "auction_type": "offer", "price": price, "seen_at": 1}


def request(item, price, loc="martlock", q=1):
    return {"item_id": item, "location": loc, "quality": q, "auction_type": "request", "price": price, "seen_at": 1}


class ChainTest(unittest.TestCase):
    def params(self, **kw):
        base = dict(buy_markets=["martlock", "lymhurst"], craft_city="", refine_city="martlock",
                    sell_market="martlock", tax=0.04)
        base.update(kw)
        return ChainParams(**base)

    def test_buy_or_craft_chosen_per_node(self):
        book = PriceBook([offer("T4_LEATHER", 500), offer("T4_HIDE", 50), offer("T3_LEATHER", 100),
                          offer("T4_CLOTH", 200, "lymhurst"), offer("T4_CLOTH", 300), request("T4_BAG", 9000)])
        res = build_chain(gd(), book, "T4_BAG", 2, self.params())
        tree = res["tree"]
        self.assertEqual(tree["decision"], "craft")
        leather, cloth = tree["children"]
        # Кожа: переработка в Мартлоке с бонусом 18% + 40% (кожа) дешевле покупки за 500.
        rr = return_rate(0.58)
        self.assertEqual(leather["return_rate"], round(rr * 100, 2))
        self.assertEqual(leather["qty"], 16)
        refine_unit = (2 * 50 + 1 * 100) * (1 - rr)
        self.assertAlmostEqual(leather["craft_unit"], refine_unit, places=1)
        self.assertEqual(leather["decision"], "craft")
        # Ткань покупается там, где дешевле.
        self.assertEqual((cloth["decision"], cloth["buy_market"], cloth["buy_unit"]), ("buy", "lymhurst", 200))
        cost = leather["best_total"] + cloth["best_total"]
        self.assertAlmostEqual(res["cost"], cost, places=1)
        self.assertAlmostEqual(res["revenue"], 9000 * 0.96 * 2)
        self.assertAlmostEqual(res["profit"], res["revenue"] - res["cost"], places=1)
        # В списке покупок — сырьё выбранной ветки, а не готовая кожа.
        self.assertEqual({s["item_id"] for s in res["shopping"]}, {"T4_HIDE", "T3_LEATHER", "T4_CLOTH"})

    def test_cycle_and_missing(self):
        book = PriceBook([offer("T4_HIDE", 50), request("T4_BAG", 9000)])
        res = build_chain(gd(), book, "T4_BAG", 1, self.params())
        self.assertIsNone(res["cost"])
        self.assertIn("T4_CLOTH", res["missing"])
        self.assertIsNone(res["profit"])

        def depth(n):
            return 1 + max((depth(c) for c in n["children"]), default=0)
        self.assertLess(depth(res["tree"]), 6)       # цикл T3_LEATHER ↔ T4_LEATHER оборван

    def test_focus_raises_return_and_counts_focus(self):
        book = PriceBook([offer("T4_HIDE", 50), offer("T3_LEATHER", 100), offer("T4_CLOTH", 200)])
        plain = build_chain(gd(), book, "T4_BAG", 1, self.params())
        focus = build_chain(gd(), book, "T4_BAG", 1, self.params(focus=True))
        self.assertLess(focus["cost"], plain["cost"])
        self.assertEqual(plain["focus"], 0)
        self.assertGreater(focus["focus"], 500)       # сумка + переработка кожи
        self.assertIsNone(plain["revenue"])

    def test_item_without_recipe_and_items_list(self):
        book = PriceBook([offer("T4_HIDE", 50)])
        res = build_chain(gd(), book, "T4_HIDE", 3, self.params())
        self.assertFalse(res["has_recipe"])
        self.assertEqual(res["tree"]["decision"], "buy")
        self.assertEqual(items_in_chain(gd(), "T4_BAG"), {"T4_BAG", "T4_LEATHER", "T4_CLOTH", "T4_HIDE", "T3_LEATHER"})

    def test_station_fee_and_silver(self):
        g = gd()
        g.items["T4_BAG"]["v"] = 1000
        g.recipes["T4_BAG"]["silver"] = 100
        book = PriceBook([offer("T4_LEATHER", 10), offer("T4_CLOTH", 10), offer("T4_HIDE", 100),
                          offer("T3_LEATHER", 100)])
        res = build_chain(g, book, "T4_BAG", 1, self.params(station_fee=200, refine_city=""))
        tree = res["tree"]
        self.assertAlmostEqual(tree["station_fee"], 1000 * 0.1125 * 2, places=2)
        self.assertEqual(tree["silver"], 100)
        self.assertAlmostEqual(res["cost"], 16 * 10 + 225 + 100, places=1)


if __name__ == "__main__":
    unittest.main()


class ChainApiTest(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        from albion_trader.server import App, AppConfig, make_handler
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        data = {"version": 2, "items": {k: dict(v) for k, v in gd().items.items()},
                "recipes": gd().recipes, "cities": gd().cities}
        data["items"]["T4_BAG"]["slot"] = "bag"
        (d / "gamedata.json").write_text(json.dumps(data))
        (d / "items.json").write_text(json.dumps({"names": {"T4_BAG": {"ru": "Сумка", "en": "Bag"}}, "index": {}}))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", capture=False))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        mk = lambda i, item, loc, price, typ="offer": {
            "Id": i, "ItemTypeId": item, "LocationId": loc, "QualityLevel": 1, "EnchantmentLevel": 0,
            "UnitPriceSilver": price * 10000, "Amount": 5, "AuctionType": typ, "Expires": "2099-01-01T00:00:00"}
        self.app.ingest("marketorders.ingest", {"Orders": [
            mk(1, "T4_HIDE", "3008", 50), mk(2, "T3_LEATHER", "3008", 100), mk(3, "T4_CLOTH", "1002", 200),
            mk(4, "T4_BAG", "3008", 9000, "request")]})

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        import json
        import urllib.request
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def test_chain_endpoint(self):
        r = self.get("/api/chain?item=T4_BAG&qty=2&buy_markets=martlock,lymhurst&craft_city=martlock"
                     "&refine_city=martlock&sell_market=martlock")
        self.assertEqual((r["name"], r["tree"]["name"], r["missing"]), ("Сумка", "Сумка", []))
        self.assertGreater(r["profit"], 0)
        self.assertEqual(r["params"]["buy_markets"], ["martlock", "lymhurst"])
        # Чёрный рынок и мусор в списке рынков покупки отбрасываются.
        r = self.get("/api/chain?item=T4_BAG&buy_markets=black_market,evil&craft_city=martlock")
        self.assertEqual(r["params"]["buy_markets"], ["martlock"])

    def test_chain_needs_item_and_survives_bad_numbers(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/chain")
        self.assertEqual(cm.exception.code, 400)
        r = self.get("/api/chain?item=T4_BAG&qty=nan&quality=99&max_age=inf&station_fee=-5")
        self.assertEqual(r["qty"], 1)

    def test_catalog_search(self):
        from urllib.parse import quote
        r = self.get("/api/items?catalog=1&q=" + quote("сумка"))
        self.assertEqual([i["item_id"] for i in r["items"]], ["T4_BAG"])
        self.assertEqual(r["items"][0]["slot"], "bag")
        r = self.get("/api/items?catalog=1&slot=bag")
        self.assertEqual([i["item_id"] for i in r["items"]], ["T4_BAG"])
        r = self.get("/api/items?catalog=1&craftable=1&q=T4")
        self.assertEqual({i["item_id"] for i in r["items"]}, {"T4_BAG", "T4_LEATHER"})
