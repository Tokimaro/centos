import tempfile
import time
import unittest
from pathlib import Path

from albion_trader.capture import photon
from albion_trader.server import App, AppConfig

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb

FP = 10_000


class EconomyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.t = [int(time.time()) - 3000]
        self.app.activity.clock = lambda: self.t[0]
        self.parser = photon.PhotonParser(self.app.albion.on_request, self.app.albion.on_response,
                                          self.app.albion.on_event)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, cmd, step=60):
        self.t[0] += step
        self.parser.receive_packet(pb.packet(cmd))

    def trade(self, kind, total):
        with self.app.write_lock, self.app.conn() as c:
            c.execute("INSERT INTO my_trades(ts, kind, source, item_id, amount, unit_price, total) "
                      "VALUES (?, ?, 'instant', 'T4_BAG', 1, ?, ?)", (self.t[0], kind, total, total))

    def test_full_accounting(self):
        self.feed(pb.response(2, {0: 5, 2: "Hero", 8: "3008", 33: 100000 * FP}))      # баланс при входе
        self.feed(pb.event(61, {0: 5}), step=1)                                       # сбор…
        self.feed(pb.event(82, {2: 1000 * FP, 5: True, 10: 100 * FP, 17: 0.2}), step=1)   # …слава: (1000+500+100)*1.2
        self.feed(pb.event(82, {2: 400 * FP}))                                        # бой
        self.feed(pb.event(62, {0: 5, 3: 1000 * FP, 4: 100 * FP, 5: 50 * FP, 7: True}))
        self.feed(pb.event(62, {0: 9, 3: 7777 * FP}))                                 # чужое
        self.feed(pb.event(279, {1: "Mob", 2: "Hero", 3: True, 5: 300}))              # серебро из лута
        self.feed(pb.event(279, {1: "Mob", 2: "Friend", 3: True, 5: 900}))            # чужой лут
        self.t[0] += 60
        self.trade("sell", 2000)                                                      # продажа (налог 4%)
        self.trade("buy", 500)
        # 100000 + 850 + 300 + 1920 − 500 − 200 (ремонт) = 102370
        self.feed(pb.event(81, {1: 102370 * FP}))
        r = self.app.api_economy({"period": "session"})
        f = r["fame"]
        self.assertEqual((f["total"], f["base"], f["premium"], f["satchel"], f["bonus"]), (2320, 1400, 500, 100, 320))
        self.assertEqual(f["by_source"], {"combat": 400, "gathering": 1920, "crafting": 0, "fishing": 0})
        s = r["silver"]
        self.assertEqual((s["mobs_net"], s["mobs_gross"], s["cluster_tax"], s["guild_tax"], s["loot"]),
                         (850, 1000, 100, 50, 300))
        self.assertEqual(r["market"], {"sales_net": 1920, "purchases": 500})
        self.assertEqual(r["balance"], {"start": 100000, "end": 102370, "change": 2370, "explained": 2570, "other": -200})
        self.assertEqual(r["income"], 850 + 300 + 1920)
        self.assertGreater(r["hours"], 0)
        self.assertEqual(sum(h["fame"] for h in r["hourly"]), 2320)
        self.assertEqual(r["series_bucket"], 3600)
        self.assertEqual(r["daily"][0]["silver"], 1150)
        week = self.app.api_economy({"period": "7"})
        self.assertEqual((week["fame"]["total"], week["balance"]["other"]), (2320, -200))
        self.assertEqual(self.app.api_economy({"period": "today"})["period"], "today")
        self.assertEqual(self.app.api_economy({"period": "nan"})["fame"]["total"], 2320)

    def test_no_balance_no_other(self):
        self.feed(pb.response(2, {0: 5, 2: "Hero", 8: "3008"}))
        self.feed(pb.event(82, {2: 10 * FP}))
        r = self.app.api_economy({})
        self.assertIsNone(r["balance"])
        self.assertEqual(r["fame"]["total"], 10)


if __name__ == "__main__":
    unittest.main()


class SeriesTest(unittest.TestCase):
    def test_gaps_filled_and_daily_for_long_periods(self):
        from albion_trader import economy
        h = {0: {"fame": 1, "silver": 2}, 7200: {"fame": 3, "silver": 0}}
        self.assertEqual([p["ts"] for p in economy._series(h)], [0, 3600, 7200])
        self.assertEqual(economy._series(h)[1], {"ts": 3600, "fame": 0, "silver": 0})
        long = {0: {"fame": 1, "silver": 0}, 3600: {"fame": 1, "silver": 0}, 5 * 86400: {"fame": 5, "silver": 1}}
        series = economy._series(long)
        self.assertEqual(economy._bucket(long), 86400)
        self.assertEqual([p["fame"] for p in series], [2, 0, 0, 0, 0, 5])
        self.assertEqual(economy._series({}), [])
