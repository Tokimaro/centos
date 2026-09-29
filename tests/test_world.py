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

TICKS = lambda ts: ts * 10_000_000 + 621_355_968_000_000_000


def event(code, params):
    return pb.packet(pb.command(4, bytes([1]) + pb.params({**params, 252: code})))


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.parser = photon.PhotonParser(self.app.albion.on_request, self.app.albion.on_response,
                                          self.app.albion.on_event)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bandit_phases_and_alerts(self):
        end = int(time.time()) + 1800
        self.parser.receive_packet(event(480, {0: TICKS(end), 1: 1}))
        self.parser.receive_packet(event(480, {0: TICKS(end), 1: 1}))            # повтор — без нового оповещения
        self.parser.receive_packet(event(480, {0: TICKS(end + 600), 1: 3, 2: ["DUCHY_RED_01"]}))
        w = self.app.api_world({})
        self.assertEqual((w["bandit"]["phase"], w["bandit"]["end_ts"]), (3, end + 600))
        self.assertEqual(w["bandit"]["data"]["provinces"], ["DUCHY_RED_01"])
        titles = [a["title"] for a in self.app.api_alerts({})["alerts"]]
        self.assertEqual(titles, ["Нападение бандитов", "Нападение бандитов"])

    def test_festivals(self):
        now = int(time.time())
        self.parser.receive_packet(event(519, {0: [1, 1], 1: ["season", "holiday"], 2: ["FEST_A", "FEST_B"],
                                               3: [TICKS(now - 3600), TICKS(now + 3600)],
                                               4: [TICKS(now + 7200), TICKS(now + 9000)]}))
        w = self.app.api_world({})
        self.assertEqual([f["name"] for f in w["festivals"]], ["FEST_A", "FEST_B"])
        self.assertEqual(w["festivals"][0]["data"]["category"], "season")
        self.assertEqual(len(self.app.api_alerts({})["alerts"]), 2)

    def test_bad_festival_payload_ignored(self):
        self.parser.receive_packet(event(519, {2: ["X"], 3: [], 4: []}))
        self.assertEqual(self.app.api_world({})["festivals"], [])
