import json
import tempfile
import time
import unittest
from pathlib import Path

from albion_trader import dungeons
from albion_trader.capture import photon
from albion_trader.gamedata import GameData
from albion_trader.server import App, AppConfig

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb

FP = 10_000
CLUSTERS = {
    "3008": ["Martlock", "PLAYERCITY_SAFEAREA_01", []],
    "4206": ["Tharcal Fissure", "OPENPVP_YELLOW", []],
    "DNG-KPR-02-MAIN-04": ["Keeper dungeon", "DUNGEON_BLACK_2", []],
    "DNG-KPR-02-MAIN-05": ["Keeper dungeon 2", "DUNGEON_BLACK_2", []],
    "CORRUPT-001": ["Corrupted Lair", "CORRUPTED_DUNGEON_INTERMEDIATE", []],
    "HELLGATE-01-2v2-01": ["The Plains", "DUNGEON_HELL_2V2_LETHAL", []],
    "Mine-SOLO": ["Curious Excavation", "T3_EXPEDITION_STANDARD", []],
    "TNL-001": ["Ouyos-Aoeuam", "TUNNEL_ROYAL", []],
}


class KindTest(unittest.TestCase):
    def test_dungeon_kind(self):
        g = GameData({"items": {}, "recipes": {}, "clusters": CLUSTERS})
        cases = {"DNG-KPR-02-MAIN-04": "static", "CORRUPT-001": "corrupted", "HELLGATE-01-2v2-01": "hellgate",
                 "Mine-SOLO": "expedition", "3008": None, "4206": None, "TNL-001": None, "TNL-999": None,
                 "5003": None, "BLACKBANK-2310": None, "@ISLAND@abc": None, "3013-Auction2": None,
                 "RandomDungeon-XYZ": "instance", "MISTS-SOLO-01": "mists", "DNG-UNKNOWN": "static", "": None}
        for zone, kind in cases.items():
            self.assertEqual(dungeons.dungeon_kind(g, zone), kind, zone)


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "gamedata.json").write_text(json.dumps({"version": 2, "items": {}, "recipes": {}, "clusters": CLUSTERS}))
        (d / "i.json").write_text(json.dumps({"names": {"T6_BAG": {"ru": "Сумка", "en": "Bag"}}, "index": {"200": "T6_BAG"}}))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.t = [int(time.time()) - 7200]
        self.app.activity.clock = lambda: self.t[0]
        self.parser = photon.PhotonParser(self.app.albion.on_request, self.app.albion.on_response,
                                          self.app.albion.on_event)
        self.app.ingest("marketorders.ingest", {"Orders": [
            {"Id": 1, "ItemTypeId": "T6_BAG", "LocationId": "3008", "QualityLevel": 1, "UnitPriceSilver": 5000 * FP,
             "Amount": 1, "AuctionType": "offer", "Expires": "2099-01-01T00:00:00"}]})

    def tearDown(self):
        self.tmp.cleanup()

    def join(self, zone, step=60):
        self.t[0] += step
        self.parser.receive_packet(pb.packet(pb.response(2, {0: 5, 2: "Hero", 8: zone})))

    def ev(self, code, params, step=30):
        self.t[0] += step
        self.parser.receive_packet(pb.packet(pb.event(code, params)))

    def test_runs_from_capture(self):
        self.join("3008")
        self.join("DNG-KPR-02-MAIN-04")
        self.ev(82, {2: 1000 * FP})                                       # слава
        self.ev(62, {0: 5, 3: 500 * FP, 4: 50 * FP})                      # серебро с моба (своё)
        self.ev(62, {0: 6, 3: 999 * FP})                                  # чужое серебро — не считаем
        self.ev(393, {0: 11, 3: "CHEST_BOSS", 21: 3})                     # легендарный сундук появился…
        self.ev(393, {0: 12, 3: "STATIC_CHEST", 23: 1})                   # …и необычный в статичном данже
        self.ev(395, {0: 11})
        self.ev(395, {0: 12})
        self.ev(395, {0: 99})                                             # неизвестный сундук — пропуск
        self.ev(279, {1: "Mob", 2: "Hero", 3: False, 4: 200, 5: 2})       # лут: 2 сумки
        self.join("DNG-KPR-02-MAIN-05")                                   # следующий уровень — то же прохождение
        self.ev(82, {2: 500 * FP})
        self.ev(165, {2: "Hero", 10: "Boss"})
        self.join("4206", step=600)                                       # вышли
        self.join("CORRUPT-001")                                          # второе прохождение, ещё идёт
        self.ev(82, {2: 300 * FP})
        res = self.app.api_dungeons({"days": "1"})
        runs = res["runs"]
        self.assertEqual([r["kind"] for r in runs], ["corrupted", "static"])
        cur, done = runs
        self.assertTrue(cur["active"])
        self.assertEqual(cur["fame"], 300)
        self.assertEqual(done["zones"], ["DNG-KPR-02-MAIN-04", "DNG-KPR-02-MAIN-05"])
        self.assertEqual(done["zone_names"], ["Keeper dungeon", "Keeper dungeon 2"])
        self.assertEqual((done["fame"], done["silver"], done["loot_value"], done["deaths"]), (1500, 450, 10000, 1))
        self.assertEqual(done["chests"], {"3": 1, "1": 1})
        self.assertEqual(done["chest_total"], 2)
        self.assertEqual(done["seconds"], 30 * 9 + 60 + 30 * 2 + 600)
        self.assertEqual(done["income"], 450 + 10000)
        by = {s["kind"]: s for s in res["summary"]}
        self.assertEqual((by["static"]["runs"], by["static"]["name"]), (1, "статичный данж"))
        self.assertEqual(res["rarity"][3], "легендарный")

    def test_session_break_closes_run(self):
        self.join("3008")
        self.join("HELLGATE-01-2v2-01")
        self.ev(82, {2: 100 * FP})
        self.app.api_session_new({}, {})
        self.join("3008")
        runs = self.app.api_dungeons({})["runs"]
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0]["active"])
        self.assertEqual(runs[0]["kind_name"], "хеллгейт")


if __name__ == "__main__":
    unittest.main()
