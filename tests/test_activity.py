import json
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


def event(code, params):
    return pb.packet(pb.command(4, bytes([1]) + pb.params({**params, 252: code})))


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        items = {"names": {"T4_BAG": {"ru": "Сумка", "en": "Bag"}}, "index": {"100": "T4_BAG"}}
        (d / "i.json").write_text(json.dumps(items))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.parser = photon.PhotonParser(self.app.albion.on_request, self.app.albion.on_response,
                                          self.app.albion.on_event)
        self.feed(pb.response(2, {2: "Hero", 8: "3008"}))

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, cmd_or_packet):
        pkt = cmd_or_packet if cmd_or_packet[:2] == b"\x00\x07" else pb.packet(cmd_or_packet)
        self.parser.receive_packet(pkt)

    def test_session_totals_and_loot_value(self):
        now = int(time.time())
        self.app.ingest("marketorders.ingest", {"Orders": [
            {"Id": 1, "ItemTypeId": "T4_BAG", "LocationId": "0007", "QualityLevel": 1, "UnitPriceSilver": 3000 * FP,
             "Amount": 1, "AuctionType": "offer", "Expires": "2099-01-01T00:00:00"}]})
        self.feed(event(82, {1: 500000 * FP, 2: 1200 * FP}))
        self.feed(event(82, {1: 501200 * FP, 2: 800 * FP}))
        self.feed(event(62, {3: 1000 * FP, 4: 50 * FP, 5: 100 * FP}))
        self.feed(event(81, {1: 20000 * FP}))
        self.feed(event(81, {1: 25000 * FP}))
        self.feed(event(279, {1: "Mob", 2: "Hero", 3: False, 4: 100, 5: 2}))
        self.feed(event(279, {1: "Mob", 2: "Friend", 3: False, 4: 100, 5: 1}))
        self.feed(event(279, {1: "Mob", 2: "Hero", 3: True, 5: 700}))
        self.feed(event(165, {2: "Hero", 10: "Killer"}))
        self.feed(event(164, {2: "Victim"}))
        rep = self.app.api_session({})["report"]
        self.assertEqual((rep["fame"], rep["silver"], rep["balance_change"]), (2000, 850, 5000))
        self.assertEqual((rep["loot_value"], rep["loot_silver"], rep["deaths"], rep["kills"]), (6000, 700, 1, 1))
        self.assertEqual(rep["items"][0]["name"], "Сумка")
        self.assertEqual(rep["character"], "Hero")
        self.assertGreater(rep["fame_per_hour"], 0)
        self.assertEqual(rep["zones"][0]["location"], "3008")

        loot = self.app.api_loot({})
        players = {p["player"]: p for p in loot["players"]}
        self.assertEqual((players["Hero"]["items"], players["Hero"]["value"], players["Hero"]["silver"]), (2, 6000, 700))
        self.assertEqual(players["Friend"]["value"], 3000)

        char = self.app.api_character({})
        self.assertEqual(char["fame_days"][0]["fame"], 2000)
        self.assertEqual(char["fame_total"][-1][1], 501200)
        self.assertEqual([b[1] for b in char["balance"]], [20000, 25000])
        self.assertEqual((char["deaths"], char["kills"]), (1, 1))

    def test_new_session_resets(self):
        self.feed(event(82, {2: 100 * FP}))
        old = self.app.activity.session_id
        new = self.app.api_session_new({}, {})["id"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.app.api_session({})["report"]["fame"], 0)
        self.assertEqual(self.app.api_session({"id": str(old)})["report"]["fame"], 100)

    def test_respec_and_stats(self):
        self.feed(event(84, {0: [0, 1500 * FP], 2: 10 * FP}))
        self.feed(event(143, {1: [1, 2], 2: [5, 6]}))
        char = self.app.api_character({})
        self.assertEqual(char["respec"]["points"], 1500)
        self.assertEqual(char["stats"]["data"]["1"], [1, 2])
