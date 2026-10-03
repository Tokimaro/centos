"""Юнит-тесты радара: граничные случаи разбора, справочников, перемотки, схем зон,
окон и API сервера (дополняют сценарные тесты test_radar.py и test_zonemaps.py)."""

import json
import struct
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock

from albion_trader import radar as radar_mod
from albion_trader import window
from albion_trader.capture import opcodes as opcodes_mod
from albion_trader.capture.albion import EVENT_MOVE, AlbionState
from albion_trader.capture.sniffer import CaptureError
from albion_trader.radar import (DEPLETED_KEEP, ENCOUNTER_GAP, STALE_AFTER, Radar, as_position, describe,
                                 find_position, resource_kind)
from albion_trader.radar_data import (CodeGuesser, MobTable, compact_mobs, guess_event, player_power,
                                      pretty_mob)
from albion_trader.radar_replay import RadarReplay, read_pcap_timed
from albion_trader.server import ApiError, App, AppConfig, make_handler
from albion_trader.zonemaps import (ZONEMAP_VERSION, ZoneMaps, assemble, classify, parse_cluster, parse_template,
                                    parse_world_index)

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb


def move_block(x, y):
    return b"\x03" + b"\x00" * 8 + struct.pack("<ff", x, y) + b"\x00" * 8


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeState:
    """Минимальный AlbionState для Radar без сети и сокетов."""

    def __init__(self):
        self.ev = {"leave": 1, "move": 3}
        self.moves_until = 0.0
        self.listeners = {}

    def on(self, name, fn):
        self.listeners.setdefault(name, []).append(fn)


# --- radar.py: помощники -----------------------------------------------------
class PositionHelpersTest(unittest.TestCase):
    def test_as_position_variants(self):
        self.assertEqual(as_position((1.0, 2.0, 3.0)), (1.0, 2.0))
        self.assertEqual(as_position(bytearray(move_block(4.0, -5.5))), (4.0, -5.5))
        self.assertIsNone(as_position([1.0]))
        self.assertIsNone(as_position([1e6, 0.0]))                        # за пределами мира
        self.assertIsNone(as_position(b"\x00" * 9 + struct.pack("<ff", float("inf"), 0.0)))
        self.assertIsNone(as_position("1,2"))

    def test_find_position_candidates_and_fallback(self):
        p = {0: 5, 3: [1.0, 2.0], 9: [7.0, 8.0], 251: [9.0, 9.0], 1: move_block(3.0, 3.0)}
        self.assertEqual(find_position(p, [9]), (7.0, 8.0))
        self.assertEqual(find_position(p, 9), (7.0, 8.0))                  # одиночный ключ
        self.assertEqual(find_position(p, [42]), (1.0, 2.0))               # поиск по всем, байты — нет
        self.assertEqual(find_position(p, [1]), (3.0, 3.0))                # байтовый блок — по явному ключу
        self.assertIsNone(find_position({0: 1, 252: [1.0, 1.0]}))

    def test_describe(self):
        self.assertEqual([describe(v) for v in (True, 3, 1.5, "a", {}, None)],
                         ["bool", "int", "float", "str", "dict", "NoneType"])
        self.assertEqual(describe(move_block(1.0, 1.0)), "bytes[25]·pos")
        self.assertEqual(describe(b"\x01\x02"), "bytes[2]")
        self.assertEqual(describe([1.0, 2.0]), "list[2]·pos")
        self.assertEqual(describe([1, 2, 3]), "list[3]")

    def test_resource_kind(self):
        self.assertEqual([resource_kind(t)[0] for t in (0, 5, 6, 10, 11, 15, 16, 22, 23, 27, 28, None, "x")],
                         ["wood", "wood", "rock", "rock", "fiber", "fiber", "hide", "hide", "ore", "ore",
                          "other", "other", "other"])


# --- radar.py: обработчики ------------------------------------------------------
class RadarHandlersTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.r = Radar(clock=self.clock)
        self.r.on_join({0: 1, 2: "Me", 9: [0.0, 0.0]})
        self.r.on_zone("0201")

    def ev(self, name, params):
        self.r.on_event(name, params)

    def test_attach_subscribes_and_touch_enables_moves(self):
        st = FakeState()
        self.r.attach(st)
        self.assertIn("event:leave", st.listeners)
        for name in ("event", "response:join", "request:move", "zone"):
            self.assertIn(name, st.listeners)
        self.r.touch()
        self.assertEqual(st.moves_until, self.clock.t + radar_mod.ACTIVE_WINDOW)
        st.listeners["zone"][0]("3004", "0201")
        self.assertEqual(self.r.me["zone"], "3004")

    def test_join_clears_and_keeps_name(self):
        self.ev("new_mob", {0: 9, 1: 1, 7: [1.0, 1.0]})
        self.r.on_join({0: 2, 9: [5.0, 6.0]})
        self.assertEqual((self.r.entities, self.r.me["id"], self.r.me["name"], self.r.me["x"]), ({}, 2, "Me", 5.0))

    def test_own_move_teleport_and_own_id_move(self):
        self.r.on_own_move({1: [3.0, 4.0]})
        self.assertEqual((self.r.me["x"], self.r.me["y"]), (3.0, 4.0))
        self.r.on_own_move({1: "bad"})
        self.assertEqual(self.r.me["x"], 3.0)
        self.ev("move", {0: 1, 1: move_block(8.0, 9.0)})                  # событие движения с моим id
        self.assertEqual(self.r.me["x"], 8.0)
        self.ev("new_character", {0: 5, 1: "Bob", 12: [1.0, 1.0]})
        self.ev("teleport", {0: 5, 1: [50.0, 60.0]})
        self.assertEqual((self.r.entities[5].x, self.r.entities[5].y), (50.0, 60.0))
        self.ev("move", {0: 5, 4: 70.0, 5: 80.0})                          # старый формат: x, y отдельно
        self.assertEqual(self.r.entities[5].x, 70.0)
        self.ev("move", {0: 5})                                            # без позиции — игнор
        self.assertEqual(self.r.entities[5].x, 70.0)

    def test_health_and_mob_state(self):
        self.ev("new_mob", {0: 9, 1: 1, 7: [1.0, 1.0], 19: 2})
        self.assertEqual(self.r.entities[9].rarity, 2)
        self.ev("health_update", {0: 9, 3: 42.0})
        self.ev("mob_change_state", {0: 9, 1: 3})
        self.ev("regeneration_health_changed", {0: 9, 2: 50.0})            # без макс. HP
        e = self.r.entities[9]
        self.assertEqual((e.health, e.enchant, e.max_health), (50.0, 3, None))
        self.ev("health_update", {0: 99, 3: 1.0})                          # нет такого — без ошибок
        self.ev("mounted", {0: 9, 11: True})                               # моб — не игрок
        self.assertIsNone(self.r.entities[9].mounted)

    def test_player_only_events_ignore_unknown(self):
        self.ev("character_equipment_changed", {0: 77, 2: [1]})
        self.ev("change_flagging_finished", {0: 77, 1: 1})
        self.ev("new_character", {0: 5, 1: "Bob", 12: [1.0, 1.0]})
        self.ev("change_flagging_finished", {0: 5})                        # без флага — не меняем
        self.ev("mounted", {0: 5, 11: "true"})
        e = self.r.entities[5]
        self.assertEqual((e.faction, e.mounted), (None, True))

    def test_harvestable_state_updates(self):
        self.ev("new_harvestable_object", {0: 10, 5: 1, 7: 4, 8: [1.0, 1.0], 10: 5})
        self.ev("harvestable_change_state", {0: 10, 1: 3, 2: 1})
        self.ev("harvestable_change_state", {0: 10})                       # ничего не пришло
        self.ev("harvestable_change_state", {0: 404, 1: 0})
        e = self.r.entities[10]
        self.assertEqual((e.size, e.enchant), (3, 1))

    def test_simple_list_edge_cases(self):
        self.ev("new_simple_harvestable_object_list", {0: [1, 2, "x"], 1: [], 2: [], 3: [1.0, 1.0, "bad", 2.0], 4: []})
        self.assertEqual(sorted(self.r.entities), [1])                     # 2 — битая позиция, 3 — нет id
        self.assertEqual(self.r.entities[1].res, "other")

    def test_generic_objects_and_chests(self):
        self.ev("new_silver_object", {0: 40, 1: [2.0, 2.0]})
        self.ev("new_portal_entrance", {0: 41, 1: [3.0, 3.0], 2: "Портал"})
        self.ev("new_something_object", {0: 42, 1: [4.0, 4.0]})
        self.ev("new_no_position", {0: 43, 1: 5})
        self.ev("update_fame", {0: 1, 1: [1.0, 1.0]})                      # не New… — не на карту
        e = self.r.entities
        self.assertEqual((e[40].kind, e[40].name), ("loot", "серебро"))
        self.assertEqual((e[41].kind, e[41].name), ("object", "Портал"))
        self.assertEqual(e[42].name, "something object")
        self.assertNotIn(43, e)
        self.ev("new_loot_chest", {0: 50, 1: [1.0, 1.0], 3: "STATIC_CHEST", 23: 3})
        self.ev("new_loot_chest", {0: 51, 1: [1.0, 1.0], 3: "CHEST", 21: 9})
        self.assertEqual((e[50].rarity, e[51].rarity), (3, None))
        self.ev("loot_chest_opened", {0: 999})

    def test_stale_entities_removed_and_age(self):
        self.ev("new_mob", {0: 9, 1: 1, 7: [1.0, 1.0]})
        self.clock.t += 30
        self.assertEqual(self.r.snapshot(touch=False)["entities"][0]["age"], 30.0)
        self.clock.t += STALE_AFTER
        self.assertEqual(self.r.snapshot(touch=False)["entities"], [])

    def test_depleted_cap_and_expiry(self):
        for i in range(305):
            self.ev("new_harvestable_object", {0: 1000 + i, 5: 25, 7: 5, 8: [float(i * 3), 0.0], 10: 1})
            self.ev("harvestable_change_state", {0: 1000 + i, 1: 0})
        self.assertEqual(len(self.r.depleted["0201"]), 300)
        self.clock.t += DEPLETED_KEEP + 1
        self.assertEqual(self.r.snapshot(touch=False)["depleted"], [])

    def test_node_seen_needs_zone_and_kind(self):
        self.r.me["zone"] = ""
        self.ev("new_harvestable_object", {0: 1, 5: 25, 7: 5, 8: [1.0, 1.0]})
        self.r.me["zone"] = "0201"
        self.ev("new_harvestable_object", {0: 2, 5: 99, 7: 5, 8: [1.0, 1.0]})   # неизвестный вид
        self.ev("new_harvestable_object", {0: 3, 5: 25, 8: [1.0, 1.0]})         # без тира
        self.assertEqual(self.r.pending_nodes, [])

    def test_encounters_skip_self_and_repeat(self):
        self.ev("new_character", {0: 1, 1: "Me", 12: [1.0, 1.0]})          # это я
        self.ev("new_character", {0: 5, 1: "", 12: [1.0, 1.0]})            # без имени
        self.ev("new_character", {0: 6, 1: "Bob", 12: [1.0, 1.0]})
        self.ev("new_character", {0: 7, 1: "Bob", 12: [1.0, 1.0]})
        self.clock.t += ENCOUNTER_GAP + 1
        self.ev("new_character", {0: 8, 1: "Bob", 12: [1.0, 1.0]})
        self.assertEqual([p["name"] for p in self.r.pending_players], ["Bob", "Bob"])

    def test_items_and_power_in_snapshot(self):
        r = Radar(clock=self.clock, item_of=lambda i: {7: "T8_2H_NATURESTAFF"}.get(i),
                  item_name=lambda iid: "посох", item_ip=lambda iid: 1200.0)
        r.on_event("new_character", {0: 5, 1: "Bob", 12: [1.0, 1.0], 40: [7, 0, 99]})
        e = r.snapshot(touch=False)["entities"][0]
        self.assertEqual(e["equipment"], [{"slot": "оружие", "id": "T8_2H_NATURESTAFF", "name": "посох"}])
        self.assertEqual((e["ip"], e["role"], e["flag"]), (1200, "хил", ""))

    def test_params_file_and_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "radar.json"
            f.write_text(json.dumps({"params": {"new_mob": {"position": [9]}}, "mob_offset": 5}))
            r = Radar(f)
            self.assertEqual((r.keys("new_mob")["position"], r.mob_offset), ([9], 5))
            f.write_text("{not json")
            r = Radar(f)
            self.assertEqual((r.keys("new_mob")["position"], r.mob_offset), ([7, 8], 0))

    def test_raw_event_codes_limit(self):
        for code in range(radar_mod.MAX_CODES + 10):
            self.r.on_raw_event(code, {0: 1})
        self.assertEqual(len(self.r.codes), radar_mod.MAX_CODES)


class RadarDbTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        radar_mod.init(self.conn)
        self.clock = Clock()
        self.r = Radar(clock=self.clock)
        self.r.on_join({0: 1, 9: [0.0, 0.0]})

    def test_zones_list_capped(self):
        for i in range(12):
            self.r.on_zone(f"Z{i}")
            self.clock.t += ENCOUNTER_GAP + 1
            self.r.on_event("new_character", {0: 5 + i, 1: "Bob", 12: [1.0, 1.0], 53: 255 if i % 2 else 0})
            self.r.flush(self.conn)
        row = radar_mod.history(self.conn, 0)[0]
        self.assertEqual((row["times"], row["hostile"], row["last_zone"]), (12, 6, "Z11"))
        self.assertEqual(row["zones"], [f"Z{i}" for i in range(2, 12)])

    def test_heat_cells(self):
        self.r.on_zone("0201")
        for i in range(3):
            self.r.on_event("new_harvestable_object", {0: i, 5: 25, 7: 5, 8: [12.0, -3.0], 10: 1})
        self.r.flush(self.conn)
        self.assertEqual(radar_mod.heat(self.conn, "0201"),
                         [{"x": 15.0, "y": -5.0, "res": "ore", "tier": 5, "enchant": 0, "seen": 3,
                           "last": int(self.clock.t)}])
        self.assertEqual(radar_mod.heat(self.conn, "other"), [])

    def test_kills_without_table_and_empty(self):
        self.assertEqual(radar_mod.kills(self.conn, []), {})
        self.assertEqual(radar_mod.kills(self.conn, ["Bob"]), {})       # нет таблицы киллборда
        self.conn.execute("CREATE TABLE kb_events (event_id INTEGER, ts INTEGER, region TEXT, participants INTEGER,"
                          " fame INTEGER, killer TEXT, victim TEXT)")
        self.conn.execute("INSERT INTO kb_events VALUES (1, 50, 'eu', 1, 1, 'X', 'Bob')")
        self.assertEqual(radar_mod.kills(self.conn, ["Bob"]), {"Bob": {"kills": 0, "deaths": 1, "last": 50}})


# --- radar_data.py ---------------------------------------------------------------
class RadarDataTest(unittest.TestCase):
    def test_compact_and_pretty(self):
        self.assertEqual(compact_mobs({"Mobs": {"Mob": {"@uniquename": "A", "@tier": "x"}}}), [["A", None, "", None]])
        self.assertEqual(compact_mobs({"Mobs": {"Mob": {"@uniquename": "A", "@hitpointsmax": "20"}}})[0][3], 20.0)
        self.assertEqual(compact_mobs(None), [])
        self.assertEqual(pretty_mob("T5_MOB_DYNAMIC_HIDE_STEPPE_TERRORBIRD"), "hide steppe terrorbird")
        self.assertEqual(pretty_mob("T8_MOB_KEEPER_BOSS_DYNAMIC"), "keeper boss")

    def test_mob_table_load_info_and_download(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mobs.json"
            t = MobTable(p)
            self.assertIsNone(t.info(1))
            p.write_text("{bad")
            self.assertFalse(t.load())
            raw = {"Mobs": {"Mob": [{"@uniquename": "T3_MOB_CRITTER_FIBER_A", "@tier": "3",
                                     "@mobtypecategory": "harmless"}]}}
            calls = []
            t = MobTable(p, fetch=lambda path: calls.append(path) or json.dumps(raw).encode())
            p.unlink()
            t.download_async()
            for _ in range(100):
                if t.rows:
                    break
                time.sleep(0.02)
            self.assertEqual(calls, ["mobs.json"])
            info = t.info(0)
            self.assertEqual((info["res"], info["category_ru"], info["boss"]), ("fiber", "мирный", False))
            self.assertIsNone(t.info(5))
            self.assertIsNone(t.info(None))
            self.assertEqual(t.info(3, offset=-3)["tier"], 3)
            t.download_async()                                               # уже есть — без запроса
            self.assertEqual(calls, ["mobs.json"])

    def test_mob_offset_guessed_by_health(self):
        # Как на записи с частного сервера: в событии тип 424 (HP 20) и 425 (HP 515),
        # а в mobs.json это кролик (408) и лиса (409) — сдвиг −16.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mobs.json"
            rows = [[f"T8_MOB_X{i}", 8, "", 1370.0 + i] for i in range(500)]
            rows[408] = ["MOB_RABBIT", 1, "harmless", 20.0]
            rows[409] = ["MOB_FOX", 1, "harmless", 515.0]
            p.write_text(json.dumps(rows))
            t = MobTable(p)
            self.assertIsNone(t.guess_offset({(424, 20.0): 4}))                 # мало мобов
            self.assertEqual(t.guess_offset({(424, 20.0): 8, (425, 515.0): 1}), -16)
            self.assertIsNone(t.guess_offset({(424, 20.0): 8, (425, 515.0): 1}, current=-16))
            r = radar_mod.Radar(mobs=t)
            for i in range(6):
                r.on_event("new_mob", {0: 100 + i, 1: 424, 7: [1.0, float(i)], 13: 20.0, 14: 20.0})
            self.assertEqual(r.mob_offset, -16)
            self.assertEqual(r.snapshot()["entities"][0]["mob"]["id"], "MOB_RABBIT")
            p.write_text(json.dumps([row[:3] for row in rows]))               # старый файл без HP
            old = MobTable(p, fetch=lambda _p: json.dumps({"Mobs": {"Mob": []}}).encode())
            self.assertFalse(old.has_hp)
            self.assertIsNone(old.guess_offset({(424, 20.0): 8}))

    def test_mob_table_download_failure(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(_):
                raise OSError("нет сети")
            t = MobTable(Path(d) / "mobs.json", fetch=boom)
            t.download_async()
            for _ in range(100):
                if not t._loading:
                    break
                time.sleep(0.02)
            self.assertIsNone(t.rows)

    def test_roles(self):
        gear = lambda w: [{"slot": "оружие", "id": w}, {"slot": "броня", "id": "A"}, {"slot": "сумка", "id": "B"}]
        ip = {"A": 800.0, "B": 2000.0}.get
        self.assertEqual(player_power(gear("T6_2H_HOLYSTAFF"), ip), {"ip": 800, "role": "хил"})
        self.assertEqual(player_power(gear("T6_2H_ARCANESTAFF"), ip)["role"], "поддержка")
        self.assertEqual(player_power(gear("T6_MAIN_MACE"), ip)["role"], "танк")
        self.assertEqual(player_power(gear("T6_2H_BOW"), ip)["role"], "урон")
        self.assertEqual(player_power([], ip), {"ip": None, "role": ""})

    def test_guess_event_shapes(self):
        pos, blk = [1.0, 2.0], move_block(1.0, 1.0)
        self.assertEqual(guess_event({0: 5, 1: blk, 252: 3}), "move")
        self.assertEqual(guess_event({0: 5, 1: "Bob", **{k: 0 for k in range(2, 16)}, 13: pos}), "new_character")
        self.assertEqual(guess_event({0: 5, 1: 7, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: pos}), "new_mob")
        self.assertEqual(guess_event({0: 5, 5: 25, 7: 6, 8: pos, 10: 1}), "new_harvestable_object")
        self.assertEqual(guess_event({0: [1, 2], 3: [1.0, 2.0, 3.0, 4.0]}), "new_simple_harvestable_object_list")
        self.assertIsNone(guess_event({0: [1, 2], 3: [1.0, 2.0]}))
        self.assertIsNone(guess_event({0: True, 1: blk}))
        self.assertIsNone(guess_event({0: 5}))
        self.assertIsNone(guess_event({0: 5, 5: 25, 7: 99, 8: pos}))         # тир вне 1–8

    def test_code_guesser_thresholds(self):
        g = CodeGuesser()
        mob = {0: 5, 1: 7, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: [1.0, 2.0]}
        for _ in range(4):
            g.observe(500, mob)
        self.assertEqual(g.suggestions({"new_mob": 123}), [])                 # мало примеров
        g.observe(500, mob)
        for _ in range(5):
            g.observe(500, {0: 1})                                            # 50% — неуверенно
        self.assertEqual(g.suggestions({"new_mob": 123}), [])
        for _ in range(20):
            g.observe(501, mob)
        self.assertEqual([s["code"] for s in g.suggestions({"new_mob": 123})], [501])
        self.assertEqual(g.suggestions({"new_mob": 501}), [])                 # уже настроено так
        for _ in range(6):
            g.observe(123, mob)                                               # настроенный код тоже верен
        self.assertEqual(g.suggestions({"new_mob": 123}), [])


# --- radar_replay.py ---------------------------------------------------------------
def pcap_bytes(packets, endian="<", nano=False, linktype=1):
    magic = 0xA1B23C4D if nano else 0xA1B2C3D4
    out = struct.pack(endian + "IHHiIII", magic, 2, 4, 0, 0, 65535, linktype)
    for ts, ip in packets:
        frame = b"\xaa" * 12 + b"\x08\x00" + ip
        frac = int((ts % 1) * (1e9 if nano else 1e6))
        out += struct.pack(endian + "IIII", int(ts), frac, len(frame), len(frame)) + frame
    return out


class ReplayUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        p = self.d / "r.pcap"
        p.write_bytes(data)
        return p

    def test_reader_errors_and_formats(self):
        with self.assertRaises(CaptureError):
            read_pcap_timed(self.write(b"\x00" * 10))
        with self.assertRaises(CaptureError):
            read_pcap_timed(self.write(b"\x0a\x0d\x0d\x0a" + b"\x00" * 30))
        ip = pb.ip_udp(pb.packet(pb.event(1, {0: 1})))
        other = pb.ip_udp(b"x", src_port=1234, dst_port=80)                   # не Albion
        for kw in ({}, {"endian": ">"}, {"nano": True}):
            got = read_pcap_timed(self.write(pcap_bytes([(10.5, ip), (11.0, other)], **kw)))
            self.assertEqual([round(t, 3) for t, _ in got], [10.5])

    def test_no_albion_traffic(self):
        with self.assertRaises(CaptureError):
            RadarReplay(self.write(pcap_bytes([])), Radar, {})

    def test_clocked_playback_speed_and_restart(self):
        ip1 = pb.ip_udp(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "0201", 9: [0.0, 0.0]})))
        ip2 = pb.ip_udp(pb.packet(pb.event(29, {0: 5, 1: "Bob", 12: [1.0, 1.0], 252: 29})))
        clock = Clock(0.0)
        rp = RadarReplay(self.write(pcap_bytes([(100.0, ip1), (110.0, ip2)])), Radar,
                         {"events": {"new_character": 29}}, clock=clock)
        rp.play()
        clock.t = 5.0
        rp.advance()
        self.assertEqual((rp.pos, len(rp.radar.entities)), (5.0, 0))
        rp.speed = 4.0
        clock.t = 7.0
        rp.advance()                                                          # +2 с × 4 = 13 > 10
        self.assertEqual((rp.pos, rp.playing, len(rp.radar.entities)), (10.0, False, 1))
        rp.play()                                                             # в конце — с начала
        self.assertEqual((rp.pos, len(rp.radar.entities)), (0.0, 0))
        rp.pause()
        clock.t = 100.0
        rp.advance()
        self.assertEqual(rp.pos, 0.0)
        self.assertEqual(rp.info()["file"], "r.pcap")
        self.assertEqual(rp.state.moves_until, float("inf"))


# --- zonemaps.py --------------------------------------------------------------------
class ZoneMapsUnitTest(unittest.TestCase):
    def test_classify_sizes(self):
        self.assertEqual(classify("SWAMP_GREEN_WALKWAY_STONE_20M_STRAIGHT_A"), ("road", 20.0, 6.0))
        self.assertEqual(classify("SWAMP_GREEN_WALKING_PLANKS_A"), ("path", 5.0, 3.0))
        self.assertEqual(classify("HIGHLAND_RED_PLATEAU_8M_STRAIGHT_A"), ("cliff", 8.0, 8.0))
        self.assertEqual(classify("HIGHLAND_RED_PLATEAU_12M_STRAIGHT_A"), ("cliff", 12.0, 8.0))
        self.assertEqual(classify("FOREST_RED_TREE_GIANT_40x40"), ("tree", 12.0, 12.0))
        self.assertEqual(classify("SWAMP_GREEN_WATER_river_30x"), ("water", 30.0, 30.0))
        self.assertIsNone(classify("SOMETHING_UNKNOWN"))

    def test_template_rotation_scale_and_bad_numbers(self):
        t = parse_template("""<template editorBoundsMin="a b" editorBoundsMax="1 1"><tiles><layergroup>
          <layer id="" name="x">
            <tile name="ROAD_DIAGONAL_RIGHT_A" pos="1 0 1" roty="10" />
            <tile name="ROAD_DIAGONAL_LEFT_A" pos="1 0 1" rot="0 10 0" />
            <tile name="SWAMP_GROUND_10x10" pos="0 0 0" scale="2 1 3" />
          </layer></layergroup></tiles></template>""")
        rots = [x[5] for x in t["tiles"]]
        self.assertEqual(rots[:2], [325, 55])
        self.assertEqual(t["tiles"][2][3:5], [20.0, 30.0])
        self.assertEqual(t["bounds"], [0.0, 0.0, 1.0, 1.0])

    def test_assemble_missing_template_cap_and_origin(self):
        cluster = parse_cluster('<cluster origin="-10 -20" size="100 200">'
                                '<templateinstance ref="NOPE" pos="0 0 0" rot="0 90 0" /></cluster>')
        self.assertEqual(cluster["instances"][0]["rot"], 90.0)
        m = assemble(cluster, {})
        self.assertEqual((m["bounds"], m["tiles"]), ([-10.0, -20.0, 90.0, 180.0], []))
        many = {"layers": [], "included": [], "exits": [], "bounds": [0, 0, 0, 0],
                "tiles": [["tree", 0, 0, 1, 1, 0, 0, -1]] * 5 + [["ground", 0, 0, 1, 1, 0, 0, -1]] * 5}
        c = {"instances": [{"ref": "T", "x": 0, "z": 0, "rot": 0, "active": []}], "bounds": [0, 0, 1, 1],
             "origin": [0, 0], "size": [1, 1], "height": [0, 1]}
        with mock.patch("albion_trader.zonemaps.MAX_TILES", 6):
            tiles = assemble(c, {"T": many})["tiles"]
        self.assertEqual([t[0] for t in tiles], ["ground"] * 5 + ["tree"])

    def test_world_index_edge_cases(self):
        idx = parse_world_index({"?xml": {}, "world": {"clusters": {"cluster": {
            "@id": "1", "@file": "f", "exits": {"exit": {"@pos": "1 2", "@targetid": "x@2"}}}}}})
        self.assertEqual(idx["1"]["exits"], [[1.0, 2.0, "2", ""]])
        idx = parse_world_index({"world": {"clusters": {"cluster": [{"@id": "1"}, {"@file": "f"},
                                                                    {"@id": "2", "@file": "g", "exits": {"exit": ["x"]}}]}}})
        self.assertEqual(list(idx), ["2"])

    def test_cache_corruption_template_errors_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def fetch(path):
                calls.append(path)
                if path == "cluster/world.json":
                    return json.dumps({"world": {"clusters": {"cluster": [
                        {"@id": "1", "@file": "a.xml", "@type": "OPENPVP_BLACK", "@displayname": "Black"}]}}}).encode()
                if path == "cluster/a.xml":
                    return b'<cluster><templateinstance ref="T" pos="0 0 0" /><templateinstance ref="GONE" pos="0 0 0" /></cluster>'
                if path == "templates/DEAD/T.template.xml":
                    raise urllib.error.HTTPError(path, 500, "boom", {}, None)
                raise urllib.error.HTTPError(path, 404, "nf", {}, None)
            zm = ZoneMaps(Path(d), fetch=fetch, name_of=lambda c: "—")
            self.assertEqual(zm.zone_name(""), "")
            self.assertEqual(zm.zone_name("1"), "1")                          # индекса ещё нет — не качаем
            r = zm.get("1", background=False)
            self.assertEqual(r["status"], "error")                            # 500 — ошибка скачивания
            self.assertIn("не удалось скачать", r["error"])
            # Зона чёрная → шаблоны ищутся сначала в DEAD; порядок шаблонов стабильный.
            self.assertEqual(calls[1:4], ["cluster/a.xml", "templates/DEAD/GONE.template.xml",
                                          "templates/GREEN/GONE.template.xml"])
            self.assertIn("templates/DEAD/T.template.xml", calls)
            self.assertEqual(zm.zone_name("1"), "Black")
            # Повреждённые кэши читаются как отсутствующие.
            zm.index_path.write_text("{bad")
            zm._index = None
            (Path(d) / "1.json").write_text("{bad")
            self.assertIsNone(zm.cached("1"))
            (Path(d) / "1.json").write_text(json.dumps({"version": ZONEMAP_VERSION - 1}))
            self.assertIsNone(zm.cached("1"))
            (Path(d) / "templates").mkdir(exist_ok=True)
            (Path(d) / "templates" / "DEAD_T.json").write_text("{bad")
            with self.assertRaises(urllib.error.HTTPError):                  # кэш битый → качаем → 500
                zm.template("T", "OPENPVP_BLACK")
            self.assertIsNone(zm.template("GONE", "OPENPVP_BLACK"))            # 404 во всех папках
            self.assertEqual(zm.index()["1"]["name"], "Black")                # индекс перекачан
            res = zm.prefetch(["1"], progress=lambda *a: calls.append(a))
            self.assertEqual(res["failed"], 1)
            self.assertEqual(calls[-1][:3], ("1", 1, 1))

    def test_http_fetch_uses_base_url(self):
        zm = ZoneMaps("/tmp/x", base_url="https://example.invalid/base")

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok"
        with mock.patch("urllib.request.urlopen", return_value=Resp()) as u:
            self.assertEqual(zm._http("cluster/x"), b"ok")
        self.assertEqual(u.call_args[0][0], "https://example.invalid/base/cluster/x")


# --- capture: фильтр движения и подписка на все события ------------------------------
class CaptureRadarHooksTest(unittest.TestCase):
    def test_accepts_event_window_and_raw_listener(self):
        clock = Clock(100.0)
        st = AlbionState(lambda *a: 0, clock=clock)
        self.assertFalse(st.accepts_event(EVENT_MOVE))
        self.assertTrue(st.accepts_event(1))
        st.moves_until = 150.0
        self.assertTrue(st.accepts_event(EVENT_MOVE))
        clock.t = 151.0
        self.assertFalse(st.accepts_event(EVENT_MOVE))
        seen = []
        st.on("event", lambda code, p: seen.append(code))
        st.on_event(1, {252: 9999})                                           # неизвестное событие
        self.assertEqual(seen, [9999])

    def test_update_opcodes_downloads_and_writes(self):
        names = list(opcodes_mod.OPERATION_NAMES.values())
        ops = "opUnused OperationType = iota\n" + "\n".join(names) + "\n)"
        evs = "evUnused EventType = iota\nevLeave\nevJoinFinished\nevMove\n)"

        class Resp:
            def __init__(self, text):
                self.text = text

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.text.encode()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("urllib.request.urlopen", side_effect=lambda url, timeout: Resp(ops if "operations" in url else evs)):
            res = opcodes_mod.update(Path(d) / "sub" / "opcodes.json")
            saved = json.loads((Path(d) / "sub" / "opcodes.json").read_text())
        self.assertEqual(saved["events"]["move"], 3)
        self.assertIn("evNewMob", res["missing"])


# --- window.py -----------------------------------------------------------------------------
class WindowUnitTest(unittest.TestCase):
    def test_radar_window_command_and_overlay_on_windows(self):
        w = window.CompanionWindow("http://127.0.0.1:8484", "/tmp/prof", page=window.RADAR_PAGE,
                                   title=window.RADAR_TITLE, size=window.RADAR_SIZE)
        self.assertEqual(w.url, "http://127.0.0.1:8484/radar.html")
        with mock.patch.object(window, "IS_WINDOWS", True), \
                mock.patch.object(window, "_set_overlay", return_value=True) as so, \
                mock.patch.object(window, "_set_topmost", return_value=False):
            r = w.set_overlay(True, 5)
            self.assertEqual((r["overlay_applied"], w.alpha), (True, 20))      # не прозрачнее 20%
            so.assert_called_with(window.RADAR_TITLE, True, 20)
            self.assertFalse(w.set_topmost(True)["applied"])
        with mock.patch.object(window, "IS_WINDOWS", True), mock.patch.object(window, "_set_overlay", return_value=False):
            self.assertIn("не найдено", w.set_overlay(False, 300)["reason"])
            self.assertEqual(w.alpha, 100)

    def test_win32_helpers_noop_elsewhere(self):
        with mock.patch.object(window, "IS_WINDOWS", False):
            self.assertFalse(window._set_overlay("x", True, 50))
            self.assertFalse(window._set_topmost("x", True))
            self.assertFalse(window._activate("x"))


# --- server.py: API радара ----------------------------------------------------------------
class ServerRadarApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=self.d / "m.db", items_path=self.d / "i.json", capture=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_api_selects_radar(self):
        self.app.radar_window.elevated = lambda: False
        self.app.radar_window.popen = mock.Mock()
        self.app.radar_window.finder = lambda: None
        self.app.radar_window.opener = mock.Mock()
        r = self.app.api_window_post({}, {"which": "radar", "action": "open", "overlay": True, "alpha": "x"})
        self.app.radar_window.opener.assert_called_once()
        self.assertEqual((r["mode"], r["overlay"], self.app.radar_window.alpha), ("browser", True, 75))
        self.assertFalse(self.app.api_window({})["running"])

    def test_zone_type_and_zonemap_with_image(self):
        self.app.zonemaps.save_index({"world": {"clusters": {"cluster": [
            {"@id": "0201", "@file": "a", "@type": "OPENPVP_RED"}]}}})
        self.assertEqual(self.app._zone_type("0201"), "OPENPVP_RED")
        self.assertEqual(self.app._zone_type("nope"), "")
        maps = self.d / "maps"
        maps.mkdir()
        (maps / "0201.webp").write_bytes(b"RIFF")
        (maps / "0201.json").write_text("{bad")
        self.app.zonemaps.fetch = lambda p: (_ for _ in ()).throw(urllib.error.HTTPError(p, 404, "nf", {}, None))
        out = self.app.api_zonemap({"zone": "0201"})
        self.assertEqual(out["image"]["kind"], "game")
        self.assertIsNone(out["image"]["bounds"])
        self.app.api_zonemap({"zone": "0201", "retry": "1"})
        self.assertIsNone(self.app.map_image(""))

    def test_resource_item_names(self):
        self.assertEqual(App.resource_item("ore", 4, 0), "T4_ORE")
        self.assertEqual(App.resource_item("hide", 6, 2), "T6_HIDE_LEVEL2@2")
        self.assertIsNone(App.resource_item("other", 4, 0))
        self.assertIsNone(App.resource_item("ore", None, 0))

    def test_mob_download_triggered_only_with_capture(self):
        self.app.radar.on_event("new_mob", {0: 9, 1: 1, 7: [1.0, 1.0]})
        with mock.patch.object(self.app.mobs, "download_async") as dl:
            self.app.api_radar({})
            dl.assert_not_called()
            self.app.config.capture = True
            self.app.api_radar({})
            dl.assert_called_once()

    def test_codes_api_validation(self):
        (self.d / "opcodes.json").write_text("{bad")
        self.app.config.opcodes_path = self.d / "opcodes.json"
        out = self.app.api_radar_codes_post({}, {"apply": {"new_mob": 555, "not_an_event": 1, "move": "x"}})
        self.assertEqual(out["events"], {"new_mob": 555})
        self.assertEqual(self.app.albion.ev["new_mob"], 555)
        with self.assertRaises(ApiError):
            self.app.api_radar_codes_post({}, {"mob_offset": "abc"})
        (self.d / "radar.json").write_text("{bad")
        self.assertEqual(self.app.api_radar_codes_post({}, {"mob_offset": "7"})["mob_offset"], 7)
        self.assertEqual(self.app.api_radar_codes_post(None, None), {})

    def test_alert_requires_name(self):
        with self.assertRaises(ApiError):
            self.app.api_radar_alert_post({}, {"text": "x"})

    def test_replay_api_edges(self):
        rec = self.d / "elsewhere.pcap"
        ip = pb.ip_udp(pb.packet(pb.event(1, {0: 1})))
        rec.write_bytes(pcap_bytes([(1.0, ip), (60.0, ip)]))
        self.app.config.record_path = str(rec)
        (self.d / "empty.pcap").write_bytes(pcap_bytes([]))
        names = [f["name"] for f in self.app.api_radar_replay({})["files"]]
        self.assertEqual(sorted(names), ["elsewhere.pcap", "empty.pcap"])
        self.assertFalse(self.app.api_radar_replay_post({}, {"action": "play"})["active"])   # нечего играть
        with self.assertRaises(ApiError):
            self.app.api_radar_replay_post({}, {"open": "empty.pcap"})
        st = self.app.api_radar_replay_post({}, {"open": "elsewhere.pcap"})
        self.assertTrue(st["playing"])
        st = self.app.api_radar_replay_post({}, {"action": "pause", "speed": 1000})
        self.assertEqual((st["playing"], st["speed"]), (False, 64.0))
        self.assertEqual(self.app.api_radar_replay_post({}, {"action": "play"})["playing"], True)

    def test_maps_http_and_local_only_codes(self):
        maps = self.d / "maps"
        maps.mkdir()
        (maps / "0201.png").write_bytes(b"\x89PNGdata")
        (self.d / "secret.txt").write_text("secret")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/maps/0201.png") as r:
                self.assertEqual((r.status, r.headers["Content-Type"], r.read()), (200, "image/png", b"\x89PNGdata"))
            for bad in ("/maps/..%2Fsecret.txt", "/maps/../secret.txt", "/maps/nope.png"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + bad)
                self.assertEqual(cm.exception.code, 404)
            for path in ("/api/radar", "/api/radar/history", "/api/radar/heat", "/api/radar/replay", "/api/zonemap"):
                with urllib.request.urlopen(base + path) as r:
                    self.assertEqual(r.status, 200, path)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()


class Win32CallsTest(unittest.TestCase):
    """Вызовы Windows для окна радара — через подменённый ctypes (проверяются флаги и прозрачность)."""

    def fake_ctypes(self, titles):
        user32 = mock.Mock()
        user32.IsWindowVisible.return_value = True
        user32.GetWindowTextLengthW.return_value = 40
        user32.GetWindowLongW.return_value = 0x100
        state = {"titles": titles}

        def enum(cb, _):
            for hwnd, _title in enumerate(state["titles"], 1):
                cb(hwnd, 0)
            return True
        user32.EnumWindows.side_effect = enum

        def get_text(hwnd, buf, n):
            buf.value = state["titles"][hwnd - 1]
        user32.GetWindowTextW.side_effect = get_text
        fake = mock.Mock()
        fake.windll.user32 = user32
        fake.WINFUNCTYPE = lambda *a: (lambda f: f)
        fake.create_unicode_buffer = lambda n: mock.Mock(value="")
        fake.c_void_p = lambda v: ("ptr", v)
        wintypes = mock.Mock()
        return fake, user32, {"ctypes": fake, "ctypes.wintypes": wintypes}

    def test_overlay_flags_alpha_and_topmost(self):
        import sys
        fake, user32, mods = self.fake_ctypes(["Albion Trader — радар", "Другое окно"])
        fake.wintypes = mods["ctypes.wintypes"]
        with mock.patch.dict(sys.modules, mods), mock.patch.object(window, "IS_WINDOWS", True):
            self.assertTrue(window._set_overlay(window.RADAR_TITLE, True, 70))
            style = user32.SetWindowLongW.call_args.args[2]
            self.assertEqual(style, 0x100 | 0x80000 | 0x20)                      # LAYERED | TRANSPARENT
            self.assertEqual(user32.SetLayeredWindowAttributes.call_args.args, (1, 0, 178, 2))
            user32.SetWindowPos.assert_called()                                   # и поверх всех окон
            self.assertTrue(window._set_overlay(window.RADAR_TITLE, False, 70))
            self.assertEqual(user32.SetWindowLongW.call_args.args[2] & 0x20, 0)   # клики снова доходят
            self.assertEqual(user32.SetLayeredWindowAttributes.call_args.args[2], 255)
            self.assertTrue(window._set_topmost(window.RADAR_TITLE, True))
            self.assertEqual(user32.SetWindowPos.call_args.args[1], ("ptr", -1))
            self.assertTrue(window._activate(window.RADAR_TITLE))
            user32.SetForegroundWindow.assert_called_with(1)
            self.assertFalse(window._set_overlay("Нет такого окна", True, 50))


class GamePortsTest(unittest.TestCase):
    def test_parse_cli_env_and_defaults(self):
        from albion_trader.__main__ import game_ports
        self.assertEqual(game_ports(None, ""), (5056,))
        self.assertEqual(game_ports([5055, 5055, 5056], "1"), (5055, 5056))     # CLI важнее переменной
        self.assertEqual(game_ports(None, "5055; 5056,x,70000,0"), (5055, 5056))
        with mock.patch.dict("os.environ", {"ALBION_TRADER_GAME_PORTS": "6000"}):
            self.assertEqual(game_ports(None), (6000,))

    def test_capture_and_replay_use_configured_port(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False, game_ports=(6000,)))
            with mock.patch("albion_trader.server.Sniffer") as sn:
                app.start_capture()
            self.assertEqual(sn.call_args.kwargs["ports"], (6000,))
            ev = app.albion.ev
            ip = pb.ip_udp(pb.packet(pb.event(ev["new_character"], {0: 5, 1: "Bob", 12: [1.0, 1.0]})),
                           src_port=6000, dst_port=50000)
            (d / "rec.pcap").write_bytes(pcap_bytes([(1.0, ip), (2.0, ip)]))
            self.assertTrue(app.api_radar_replay_post({}, {"open": "rec.pcap"})["active"])
            app.api_radar_replay_post({}, {"seek": 2})
            self.assertEqual(app.api_radar({})["entities"][0]["name"], "Bob")
            app.config.game_ports = (5056,)
            app.api_radar_replay_post({}, {"action": "close"})
            with self.assertRaises(ApiError) as cm:
                app.api_radar_replay_post({}, {"open": "rec.pcap"})
            self.assertIn("UDP 5056", str(cm.exception))

    def test_serve_cli_passes_ports(self):
        from albion_trader import __main__ as cli
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cli, "serve") as serve:
            cli.main(["--data-dir", d, "serve", "--no-capture", "--game-port", "5055"])
        self.assertEqual(serve.call_args.args[0].game_ports, (5055,))
