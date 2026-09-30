import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from albion_trader import activity, destiny
from albion_trader.gamedata import GameData

DESTINY = {"templates": {"COMBAT_SPEC": [100, 200, 300, 400], "CRAFT_SPEC": [10, 20], "GATHER_T5": [5],
                         "REFINE_T4": [5]},
           "nodes": {"COMBAT_SWORDS_BROAD": {"tpl": "COMBAT_SPEC", "cat": "fighting", "mission": "killmobfame",
                                             "mult": 1, "item": "T8_MAIN_SWORD", "base": False},
                     "CRAFT_BAGS": {"tpl": "CRAFT_SPEC", "cat": "crafting", "mult": 2, "item": "T8_BAG", "base": True},
                     "GATHER_FISH": {"tpl": "CRAFT_SPEC", "cat": "gathering", "mission": "fishingfame", "mult": 1},
                     "FARM_X": {"tpl": "CRAFT_SPEC", "cat": "farming", "mult": 1},
                     "GATHER_ORE_T5": {"tpl": "GATHER_T5", "cat": "gathering", "mission": "gatherfame", "mult": 1,
                                       "item": "T8_2H_TOOL_PICK"},
                     "CRAFT_REFINE_FIBER_T4": {"tpl": "REFINE_T4", "cat": "crafting", "mission": "craftitemfame",
                                               "mult": 1, "item": "T8_CLOTH"}}}
NAMES = {"T8_MAIN_SWORD": "Палаш (старейшина)", "T8_BAG": "Сумка (старейшина)", "T8_2H_TOOL_PICK": "Кирка (старейшина)",
         "T8_CLOTH": "Великолепная ткань"}


def gd():
    return GameData({"items": {}, "recipes": {}, "destiny": DESTINY})


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    activity.init(c)
    destiny.init(c)
    return c


def fame(c, ts, value, src="combat", session=1):
    c.execute("INSERT INTO activity_events(ts, session_id, kind, value, data) VALUES (?, ?, 'fame', ?, ?)",
              (ts, session, value, json.dumps({"src": src})))


class LogicTest(unittest.TestCase):
    def test_tables_and_math(self):
        g = gd()
        self.assertEqual(destiny.level_table(g, "CRAFT_BAGS"), [20, 40])          # множитель ×2
        self.assertEqual(destiny.level_table(g, "NOPE"), [])
        t = destiny.level_table(g, "COMBAT_SWORDS_BROAD")
        self.assertEqual(destiny.remaining(t, 1, 50, 3), 200 + 300 - 50)
        self.assertEqual(destiny.remaining(t, 3, 0, 2), 0)
        self.assertEqual(destiny.advance(t, 0, 50, 300), (2, 50.0))               # 350 → уровни 1 и 2, остаток 50
        self.assertEqual(destiny.advance(t, 3, 0, 10 ** 6), (4, 0.0))             # потолок
        self.assertEqual(destiny.advance(t, 1, 10, -5), (1, 10.0))

    def test_sources_and_titles(self):
        g = gd()
        nodes = g.destiny["nodes"]
        self.assertEqual([destiny.node_source(nodes[n]) for n in ("COMBAT_SWORDS_BROAD", "CRAFT_BAGS", "GATHER_FISH", "FARM_X")],
                         ["combat", "crafting", "fishing", None])
        self.assertEqual(destiny.node_title(g, lambda i: NAMES.get(i, i), "CRAFT_BAGS"), "Ремесло: Сумка (ветка)")
        name_of = lambda i: NAMES.get(i, i)
        self.assertEqual(destiny.node_title(g, name_of, "GATHER_ORE_T5"), "Сбор: Руда (Кирка) T5")
        self.assertEqual(destiny.node_title(g, name_of, "CRAFT_REFINE_FIBER_T4"),
                         "Ремесло: Переработка (Великолепная ткань) T4")
        self.assertEqual(destiny.node_title(g, name_of, "GATHER_FISH"), "Сбор: Рыбалка")
        self.assertEqual([n["node_id"] for n in destiny.search_nodes(g, name_of, "рыбалка")], ["GATHER_FISH"])
        found = destiny.search_nodes(g, lambda i: NAMES.get(i, i), "палаш")
        self.assertEqual([n["node_id"] for n in found], ["COMBAT_SWORDS_BROAD"])
        self.assertEqual(found[0]["total_fame"], 1000)

    def test_track_clamps(self):
        c, g = conn(), gd()
        destiny.track(c, g, "COMBAT_SWORDS_BROAD", 9, 5000, 1, True, now=10)
        row = dict(c.execute("SELECT * FROM destiny_track").fetchone())
        self.assertEqual((row["level"], row["progress"], row["target"]), (4, 5000.0, 4))
        destiny.track(c, g, "COMBAT_SWORDS_BROAD", 1, 5000, 0, True, now=10)
        row = dict(c.execute("SELECT * FROM destiny_track").fetchone())
        self.assertEqual((row["level"], row["progress"], row["target"]), (1, 200.0, 2))
        with self.assertRaises(ValueError):
            destiny.track(c, g, "NOPE", 1, 0, 2, True)
        destiny.untrack(c, "COMBAT_SWORDS_BROAD")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM destiny_track").fetchone()[0], 0)

    def test_report_auto_progress_and_eta(self):
        c, g = conn(), gd()
        now = 100000
        fame(c, now - 5000, 999)                        # до отметки — не прибавляется
        destiny.track(c, g, "COMBAT_SWORDS_BROAD", 0, 50, 4, True, now=now - 3600)
        destiny.track(c, g, "CRAFT_BAGS", 0, 0, 2, False, now=now - 3600)        # не активен
        fame(c, now - 3000, 200)
        fame(c, now - 2400, 100)
        fame(c, now - 1800, 30, "crafting")
        fame(c, now - 1200, 70, "gathering")
        rep = destiny.report(c, g, lambda i: NAMES.get(i, i), now, days=1)
        sword = [r for r in rep["rows"] if r["node_id"] == "COMBAT_SWORDS_BROAD"][0]
        self.assertEqual((sword["gained"], sword["level"], sword["progress"]), (300, 2, 50))   # 50+300 = 100+200+50
        self.assertEqual(sword["remaining"], 300 + 400 - 50)
        bags = [r for r in rep["rows"] if r["node_id"] == "CRAFT_BAGS"][0]
        self.assertEqual((bags["gained"], bags["level"]), (0, 0))
        hours = 2400 / 3600          # промежутки 2000→600 (простой), 600, 600, 600
        self.assertAlmostEqual(rep["hours"], hours, places=2)
        self.assertEqual(rep["rates"]["combat"], round((999 + 300) / hours))
        self.assertAlmostEqual(sword["eta_hours"], round(650 / rep["rates"]["combat"], 1), delta=0.1)
        self.assertIsNone(rep["rates"]["fishing"])


class DestinyApiTest(unittest.TestCase):
    def setUp(self):
        from albion_trader.server import App, AppConfig, make_handler
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "gamedata.json").write_text(json.dumps({"version": 2, "items": {}, "recipes": {}, "destiny": DESTINY}))
        (d / "items.json").write_text(json.dumps({"names": {k: {"ru": v, "en": v} for k, v in NAMES.items()}, "index": {}}))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", capture=False))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, body):
        req = urllib.request.Request(self.base + "/api/destiny", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_flow(self):
        from urllib.parse import quote
        with urllib.request.urlopen(self.base + "/api/destiny?q=" + quote("сумка")) as r:
            data = json.load(r)
        self.assertTrue(data["has_data"])
        self.assertEqual(data["nodes"][0]["node_id"], "CRAFT_BAGS")
        res = self.post({"action": "track", "node": "CRAFT_BAGS", "level": 0, "progress": 5, "target": 2})
        self.assertEqual((res["rows"][0]["remaining"], res["rows"][0]["title"]), (55, "Ремесло: Сумка (ветка)"))
        res = self.post({"action": "untrack", "node": "CRAFT_BAGS"})
        self.assertEqual(res["rows"], [])
        for bad in ({"action": "track", "node": "NOPE"}, {"action": "x", "node": "CRAFT_BAGS"}, {"action": "track"},
                    {"action": "track", "node": ["x"]}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.post(bad)
            self.assertEqual(cm.exception.code, 400)
        res = self.post({"action": "track", "node": "CRAFT_BAGS", "level": "nan", "progress": "inf", "target": "x"})
        self.assertEqual(res["rows"][0]["start_level"], 0)


if __name__ == "__main__":
    unittest.main()
