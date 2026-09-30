import json
import struct
import tempfile
import unittest
from pathlib import Path

from albion_trader.capture import photon
from albion_trader.capture.albion import EVENT_MOVE, load_opcodes
from albion_trader.capture.opcodes import OPERATION_NAMES, build_opcodes
from albion_trader.radar import Radar, as_position
from albion_trader.server import App, AppConfig

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb


def event(code, params):
    return pb.packet(pb.event(code, params))


def move(obj_id, x, y):
    """Событие движения, как шлёт Albion: код Photon 3, без параметра 252."""
    block = b"\x03" + b"\x00" * 8 + struct.pack("<ff", x, y) + b"\x00" * 8
    return pb.packet(pb.command(4, bytes([EVENT_MOVE]) + pb.params({0: obj_id, 1: block})))


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        st = self.app.albion
        self.ev = st.ev
        self.parser = photon.PhotonParser(st.on_request, st.on_response, st.on_event,
                                          event_filter=st.accepts_event)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, *packets):
        for p in packets:
            self.parser.receive_packet(p)

    def radar(self):
        return self.app.api_radar({})

    def test_join_players_move_leave(self):
        self.feed(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "3004", 9: [10.0, 10.0]})))
        self.radar()   # вкладка открыта — движение разбирается
        self.feed(event(self.ev["new_character"], {0: 5, 1: "Bob", 8: "Wolves", 12: [13.0, 14.0],
                                                   22: 50.0, 23: 100.0}),
                  move(5, 7.5, -2.25),
                  pb.packet(pb.request(21, {1: [11.0, 12.0]})))
        r = self.radar()
        self.assertEqual((r["me"]["name"], r["me"]["x"], r["me"]["y"], r["me"]["zone"]), ("Me", 11.0, 12.0, "3004"))
        self.assertTrue(r["me"]["zone_name"])
        bob = r["entities"][0]
        self.assertEqual((bob["kind"], bob["name"], bob["guild"], bob["x"], bob["y"], bob["health"]),
                         ("player", "Bob", "Wolves", 7.5, -2.25, 50.0))
        self.feed(event(self.ev["leave"], {0: 5}))
        self.assertEqual(self.radar()["entities"], [])

    def test_moves_skipped_while_radar_closed(self):
        self.feed(event(self.ev["new_character"], {0: 5, 1: "Bob", 12: [1.0, 2.0]}), move(5, 9.0, 9.0))
        self.assertEqual(self.app.radar.entities[5].x, 1.0)
        self.assertTrue(self.radar()["moves"])
        self.feed(move(5, 9.0, 9.0))
        self.assertEqual(self.app.radar.entities[5].x, 9.0)

    def test_mobs_resources_loot(self):
        self.feed(
            event(self.ev["new_mob"], {0: 9, 1: 321, 7: [1.0, 1.0], 33: 2}),
            event(self.ev["new_harvestable_object"], {0: 10, 5: 12, 7: 6, 8: [2.0, 2.0], 10: 3, 11: 1}),
            event(self.ev["new_simple_harvestable_object_list"],
                  {0: [20, 21], 1: b"\x00\x18", 2: b"\x04\x05", 3: [5.0, 6.0, 7.0, 8.0], 4: b"\x02\x03"}),
            event(self.ev["new_loot_chest"], {0: 30, 1: [9.0, 9.0], 3: "Сундук"}),
            event(self.ev["new_silver_object"], {0: 31, 1: [3.0, 3.0]}),
        )
        e = self.app.radar.entities
        self.assertEqual((e[9].kind, e[9].type_id, e[9].enchant), ("mob", 321, 2))
        self.assertEqual((e[10].res, e[10].tier, e[10].size, e[10].enchant), ("fiber", 6, 3, 1))
        self.assertEqual((e[20].res, e[21].res, e[21].x, e[21].tier), ("wood", "ore", 7.0, 5))
        self.assertEqual((e[30].kind, e[30].name), ("loot", "Сундук"))
        self.assertEqual(e[31].kind, "loot")
        self.feed(event(self.ev["harvestable_change_state"], {0: 10, 1: 0}))
        self.assertNotIn(10, e)

    def test_zone_change_clears_map(self):
        self.feed(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "3004", 9: [0.0, 0.0]})),
                  event(self.ev["new_mob"], {0: 9, 1: 1, 7: [1.0, 1.0]}))
        self.assertEqual(len(self.radar()["entities"]), 1)
        self.feed(pb.packet(pb.request(17, {0: "3008"})))       # GetGameServerByCluster — переход
        self.assertEqual(self.radar()["entities"], [])

    def test_codes_diagnostics(self):
        self.feed(event(999, {0: 1, 1: [1.0, 2.0], 2: "x"}), event(999, {0: 2}))
        codes = {c["code"]: c for c in self.radar()["codes"]}
        self.assertEqual(codes[999]["count"], 2)
        self.assertIn("1:list[2]·pos", codes[999]["shape"])
        self.assertIsNone(codes[999]["name"])

    def test_opcodes_override_and_params_file(self):
        d = Path(self.tmp.name)
        (d / "opcodes.json").write_text(json.dumps({"events": {"new_mob": 777}}), encoding="utf-8")
        (d / "radar.json").write_text(json.dumps({"params": {"new_mob": {"position": [2]}}}), encoding="utf-8")
        app = App(AppConfig(db_path=d / "m2.db", items_path=d / "i.json", capture=False,
                            opcodes_path=d / "opcodes.json"))
        self.assertEqual(app.radar.keys("new_mob")["position"], [2])
        st = app.albion
        p = photon.PhotonParser(st.on_request, st.on_response, st.on_event)
        p.receive_packet(event(777, {0: 1, 1: 5, 2: [4.0, 4.0]}))
        self.assertEqual(app.radar.entities[1].x, 4.0)

    def test_update_opcodes_maps_radar_events(self):
        names = list(OPERATION_NAMES.values())
        ops = "opUnused OperationType = iota\n" + "\n".join(names) + "\n)"
        evs = "evUnused EventType = iota\nevLeave\nevJoinFinished\nevMove\nevNewMob = 130\nevNewCharacter = 31\n)"
        codes = build_opcodes(ops, evs)
        self.assertEqual(codes["move"], names.index("opMove") + 1)
        self.assertEqual((codes["events"]["new_mob"], codes["events"]["new_character"], codes["events"]["move"]),
                         (130, 31, 3))
        self.assertEqual(load_opcodes(None)["events"]["new_mob"], 123)

    def test_player_details_like_zqradar(self):
        self.app.catalog.index.update({"7": "T8_2H_FIRESTAFF", "9": "T4_MOUNT_HORSE"})
        self.feed(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "0201", 9: [0.0, 0.0]})),
                  event(self.ev["new_character"], {0: 5, 1: "Grom", 8: "Wolves", 49: "NORD", 53: 255,
                                                   12: [3.0, 4.0], 22: 900.0, 23: 2000.0, 40: [7, 0, 0, 0, 0, 0, 0, 9]}),
                  event(self.ev["new_character"], {0: 6, 1: "Lira", 53: 1, 12: [1.0, 1.0]}))
        self.feed(event(self.ev["mounted"], {0: 5, 11: True}),
                  event(self.ev["mounted"], {0: 6, 10: -1}),
                  event(self.ev["regeneration_health_changed"], {0: 5, 2: 1500.0, 3: 2100.0}),
                  event(self.ev["change_flagging_finished"], {0: 6, 1: 4}))
        players = {e["id"]: e for e in self.radar()["entities"]}
        grom, lira = players[5], players[6]
        self.assertEqual((grom["alliance"], grom["faction"], grom["flag"]), ("NORD", 255, "враждебный"))
        self.assertTrue(grom["mounted"])
        self.assertEqual((grom["health"], grom["max_health"]), (1500.0, 2100.0))
        self.assertEqual([(i["slot"], i["id"]) for i in grom["equipment"]],
                         [("оружие", "T8_2H_FIRESTAFF"), ("маунт", "T4_MOUNT_HORSE")])
        self.assertTrue(lira["mounted"])
        self.assertEqual((lira["faction"], lira["flag"]), (4, "Форт Стерлинг"))
        self.feed(event(self.ev["mounted"], {0: 5, 11: False}),
                  event(self.ev["character_equipment_changed"], {0: 5, 2: [9]}))
        grom = {e["id"]: e for e in self.radar()["entities"]}[5]
        self.assertFalse(grom["mounted"])
        self.assertEqual([i["id"] for i in grom["equipment"]], ["T4_MOUNT_HORSE"])

    def test_position_helpers(self):
        self.assertEqual(as_position([1.5, 2.5]), (1.5, 2.5))
        self.assertIsNone(as_position([1, 2]))                   # целые — не координаты
        self.assertIsNone(as_position([float("nan"), 1.0]))
        self.assertIsNone(as_position(b"\x00" * 10))
        r = Radar()
        self.assertEqual(r.keys("new_something")["id"], 0)


if __name__ == "__main__":
    unittest.main()
