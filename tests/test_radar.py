import json
import unittest.mock
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

    def test_own_move_op_detected_by_shape(self):
        # Частный сервер: свой запрос движения идёт с другим кодом операции.
        self.feed(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "3004", 9: [10.0, 10.0]})))
        self.feed(pb.packet(pb.request(77, {1: [5.0, 5.0], 2: 3})))          # редкий запрос — не движение
        for i in range(8):
            self.feed(pb.packet(pb.request(40, {1: [20.0 + i, 30.0]})))
        r = self.radar()
        self.assertEqual((r["me"]["x"], r["me"]["y"]), (27.0, 30.0))
        self.assertEqual(self.app.albion.stats["move_op_detected"], 40)
        self.feed(pb.packet(pb.request(40, {1: [1.0, 2.0]})))
        self.assertEqual(self.radar()["me"]["x"], 1.0)
        self.feed(pb.packet(pb.request(77, {1: [5.0, 5.0]})))
        self.assertEqual(self.radar()["me"]["x"], 1.0)

    def test_move_op_detected_even_if_code_is_taken(self):
        # Номера операций сдвинулись: движение пришло под кодом «рыночного» запроса,
        # а под 21 игра шлёт что-то без координат.
        self.feed(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "3004", 9: [10.0, 10.0]})))
        for i in range(10):
            self.feed(pb.packet(pb.request(21, {0: i})),
                      pb.packet(pb.request(82, {0: 123456 + i, 1: [40.0 + i, 50.0], 3: [41.0, 51.0]})))
        r = self.radar()
        self.assertEqual((r["me"]["x"], r["me"]["y"]), (49.0, 50.0))
        rq = r["requests"]
        self.assertEqual((rq["move"], rq["detected"], rq["move_seen"]), (82, True, True))
        self.assertEqual(rq["total"], 20)
        self.assertEqual(self.app.albion.stats["market_requests"], 7)     # дальше уже не рынок
        row = next(c for c in rq["codes"] if c["code"] == 82)
        self.assertEqual((row["name"], row["positions"]), ("move", 10))
        self.assertIn("1:xy", row["shape"])

    def test_known_move_op_disables_detection(self):
        self.feed(pb.packet(pb.request(21, {1: [11.0, 12.0]})))
        for i in range(10):
            self.feed(pb.packet(pb.request(40, {1: [20.0 + i, 30.0]})))
        self.assertNotIn("move_op_detected", self.app.albion.stats)
        self.assertEqual(self.radar()["me"]["x"], 11.0)

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


class RadarFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False,
                                 opcodes_path=d / "opcodes.json"))
        st = self.app.albion
        self.ev = st.ev
        self.parser = photon.PhotonParser(st.on_request, st.on_response, st.on_event, event_filter=st.accepts_event)
        self.feed(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "0201", 9: [0.0, 0.0]})))

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, *packets):
        for p in packets:
            self.parser.receive_packet(p)

    def test_chest_rarity_and_opened(self):
        self.feed(event(self.ev["new_loot_chest"], {0: 30, 1: [2.0, 2.0], 3: "CHEST_A", 21: 2}))
        self.assertEqual(self.app.radar.entities[30].rarity, 2)
        self.feed(event(self.ev["loot_chest_opened"], {0: 30}))
        self.assertTrue(self.app.api_radar({})["entities"][0]["opened"])

    def test_depleted_nodes_heat_and_values(self):
        self.app.value_of_factory = lambda conn: (lambda iid: {"T6_ORE_LEVEL1@1": 500.0}.get(iid))
        self.feed(event(self.ev["new_harvestable_object"], {0: 10, 5: 25, 7: 6, 8: [12.0, 3.0], 10: 4, 11: 1}))
        snap = self.app.api_radar({})
        ore = snap["entities"][0]
        self.assertEqual((ore["item"], ore["price"], ore["value"]), ("T6_ORE_LEVEL1@1", 500.0, 2000))
        self.feed(event(self.ev["harvestable_change_state"], {0: 10, 1: 0}))
        snap = self.app.api_radar({})
        self.assertEqual([(d["res"], d["tier"], d["enchant"]) for d in snap["depleted"]], [("ore", 6, 1)])
        cells = self.app.api_radar_heat({})["cells"]
        self.assertEqual([(c["res"], c["tier"], c["seen"]) for c in cells], [("ore", 6, 1)])
        # Узел появился снова — из истощённых пропадает.
        self.feed(event(self.ev["new_harvestable_object"], {0: 11, 5: 25, 7: 6, 8: [12.0, 3.0], 10: 4, 11: 1}))
        self.assertEqual(self.app.api_radar({})["depleted"], [])

    def test_history_kills_power_and_role(self):
        self.app.catalog.index.update({"7": "T8_2H_HOLYSTAFF", "8": "T6_ARMOR_CLOTH_SET1"})
        self.app.gamedata.items.update({"T8_2H_HOLYSTAFF": {"ip": 1100}, "T6_ARMOR_CLOTH_SET1": {"ip": 900}})
        with self.app.conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS kb_events (event_id INTEGER PRIMARY KEY, ts INTEGER, region TEXT,"
                         " participants INTEGER, fame INTEGER, killer TEXT, victim TEXT)")
            conn.execute("INSERT INTO kb_events VALUES (1, 100, 'europe', 1, 1, 'Grom', 'X')")
            conn.execute("INSERT INTO kb_events VALUES (2, 200, 'europe', 1, 1, 'Y', 'Grom')")
            conn.execute("INSERT INTO kb_events VALUES (3, 300, 'europe', 1, 1, 'Grom', 'Z')")
        self.feed(event(self.ev["new_character"], {0: 5, 1: "Grom", 8: "Wolves", 53: 255, 12: [3.0, 4.0],
                                                   40: [7, 0, 0, 8]}))
        grom = self.app.api_radar({})["entities"][0]
        self.assertEqual((grom["ip"], grom["role"]), (1000, "хил"))
        self.assertEqual((grom["kb"]["kills"], grom["kb"]["deaths"]), (2, 1))
        hist = self.app.api_radar_history({})["rows"]
        self.assertEqual((hist[0]["name"], hist[0]["times"], hist[0]["hostile"], hist[0]["zones"]),
                         ("Grom", 1, 1, ["0201"]))
        # Повторное появление в течение 10 минут — та же встреча.
        self.feed(event(self.ev["leave"], {0: 5}), event(self.ev["new_character"], {0: 6, 1: "Grom", 12: [1.0, 1.0]}))
        self.assertEqual(self.app.api_radar_history({})["rows"][0]["times"], 1)

    def test_mob_names_with_offset(self):
        from albion_trader.radar_data import MobTable
        mobs = MobTable(Path(self.tmp.name) / "mobs.json")
        mobs.save({"Mobs": {"Mob": [{"@uniquename": "T4_MOB_A", "@tier": "4"},
                                    {"@uniquename": "T8_MOB_HIDE_STEPPE_MAMMOTH", "@tier": "8",
                                     "@mobtypecategory": "boss"}]}})
        self.app.radar.mobs = mobs
        self.feed(event(self.ev["new_mob"], {0: 9, 1: 11, 7: [1.0, 1.0]}))
        self.assertIsNone(self.app.api_radar({})["entities"][0]["mob"])
        self.app.api_radar_codes_post({}, {"mob_offset": -10})
        mob = self.app.api_radar({})["entities"][0]["mob"]
        self.assertEqual((mob["name"], mob["tier"], mob["res"], mob["boss"]), ("hide steppe mammoth", 8, "hide", True))
        self.assertEqual(json.loads((Path(self.tmp.name) / "radar.json").read_text())["mob_offset"], -10)

    def test_code_guessing_and_apply(self):
        # Сервер шлёт мобов под кодом 777 вместо настроенного.
        for i in range(6):
            self.feed(event(777, {0: 100 + i, 1: 5, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: [1.0, 2.0], 13: 10.0}))
        sugg = self.app.api_radar({})["suggestions"]
        self.assertIn({"name": "new_mob", "code": 777, "current": self.ev["new_mob"], "samples": 6, "share": 1.0}, sugg)
        self.app.api_radar_codes_post({}, {"apply": {"new_mob": 777}})
        self.assertEqual(self.app.albion.ev["new_mob"], 777)
        self.assertEqual(json.loads((Path(self.tmp.name) / "opcodes.json").read_text())["events"]["new_mob"], 777)
        self.feed(event(777, {0: 200, 1: 5, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: [1.0, 2.0]}))
        self.assertIn(200, self.app.radar.entities)
        self.assertNotIn("new_mob", [s["name"] for s in self.app.api_radar({})["suggestions"]])

    def test_auto_codes(self):
        self.app.api_radar_codes_post({}, {"auto": True})
        for i in range(6):
            self.feed(event(778, {0: 300 + i, 5: 25, 7: 5, 8: [1.0, 1.0], 10: 2}))
        self.assertTrue(self.app.api_radar({})["autocodes"])
        self.assertEqual(self.app.albion.ev["new_harvestable_object"], 778)

    def test_hostile_alert_and_map_image(self):
        r = self.app.api_radar_alert_post({}, {"name": "Grom", "text": "40 м"})
        self.assertEqual(r["sent"], 1)
        self.assertEqual(self.app.api_radar_alert_post({}, {"name": "Grom"})["sent"], 0)   # повтор не шлём
        maps = Path(self.tmp.name) / "maps"
        maps.mkdir()
        (maps / "0201.png").write_bytes(b"\x89PNG")
        (maps / "0201.json").write_text('{"kind": "flat", "bounds": [-400, -400, 400, 400]}')
        img = self.app.map_image("0201")
        self.assertTrue(img["url"].startswith("maps/0201.png"))
        self.assertEqual((img["kind"], img["bounds"]), ("flat", [-400, -400, 400, 400]))
        self.assertIsNone(self.app.map_image("9999"))


def write_pcap(path, packets):
    """packets: [(время, IP-пакет)] → классический .pcap с Ethernet."""
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for ts, ip in packets:
            frame = b"\xaa" * 12 + b"\x08\x00" + ip
            f.write(struct.pack("<IIII", int(ts), int((ts % 1) * 1e6), len(frame), len(frame)) + frame)


class ReplayAndOverlayTest(unittest.TestCase):
    def test_replay_play_seek_close(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
            ev = app.albion.ev
            pk = lambda *c: pb.ip_udp(pb.packet(*c))
            write_pcap(d / "rec.pcap", [
                (1000.0, pk(pb.response(2, {0: 1, 2: "Me", 8: "0201", 9: [0.0, 0.0]}))),
                (1001.0, pk(pb.event(ev["new_character"], {0: 5, 1: "Bob", 12: [1.0, 1.0]}))),
                (1010.0, pk(pb.command(4, bytes([3]) + pb.params({0: 5, 1: b"\x03" + b"\x00" * 8 + struct.pack("<ff", 9.0, 9.0) + b"\x00" * 8})))),
                (1020.0, pk(pb.event(ev["leave"], {0: 5}))),
            ])
            files = app.api_radar_replay({})["files"]
            self.assertEqual([f["name"] for f in files], ["rec.pcap"])
            st = app.api_radar_replay_post({}, {"open": "rec.pcap"})
            self.assertEqual((st["active"], st["duration"], st["packets"]), (True, 20.0, 4))
            app.api_radar_replay_post({}, {"action": "pause"})
            app.api_radar_replay_post({}, {"seek": 5})
            snap = app.api_radar({})
            self.assertEqual(snap["replay"]["pos"], 5.0)
            self.assertEqual([(e["name"], e["x"]) for e in snap["entities"]], [("Bob", 1.0)])
            app.api_radar_replay_post({}, {"seek": 15})
            self.assertEqual(app.api_radar({})["entities"][0]["x"], 9.0)
            app.api_radar_replay_post({}, {"seek": 2})          # назад — пересборка с начала
            self.assertEqual(app.api_radar({})["entities"][0]["x"], 1.0)
            app.api_radar_replay_post({}, {"seek": 25})
            self.assertEqual(app.api_radar({})["entities"], [])
            self.assertEqual(app.api_radar({})["me"]["zone"], "0201")
            self.assertFalse(app.api_radar_replay_post({}, {"action": "close"})["active"])
            self.assertNotIn("replay", app.api_radar({}))
            # Рыночные данные и история встреч из записи не сохраняются.
            self.assertEqual(app.api_radar_history({})["rows"], [])
            with self.assertRaises(Exception):
                app.api_radar_replay_post({}, {"open": "../nope.pcap"})

    def test_overlay_not_windows(self):
        from albion_trader import window
        w = window.CompanionWindow("http://x", "/tmp/p", page=window.RADAR_PAGE, title=window.RADAR_TITLE)
        with unittest.mock.patch.object(window, "IS_WINDOWS", False):
            r = w.set_overlay(True, 60)
        self.assertEqual((r["overlay"], r["overlay_applied"], w.alpha), (True, False, 60))
