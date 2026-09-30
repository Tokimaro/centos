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


class KillsAndZonesTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from albion_trader import activity
        self.activity = activity
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        activity.init(self.conn)
        self.t0 = 1_800_000_000

    def add(self, dt, kind, session=1, **f):
        self.conn.execute(
            "INSERT INTO activity_events(ts, session_id, kind, location, item_id, amount, value, actor, target, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.t0 + dt, session, kind, f.get("location"), f.get("item_id"), f.get("amount"), f.get("value"),
             f.get("actor"), f.get("target"), json.dumps(f["data"]) if "data" in f else None))

    def test_kill_log(self):
        self.add(0, "death", location="Z", actor="Hero", target="Victim",
                 data={"victim_guild": "G1", "killer_guild": "G2"})
        self.add(0, "kill", location="Z", actor="Hero", target="Victim")      # дубль той же смерти
        self.add(60, "death", location="Z", actor="Bandit", target="Hero", data={})
        self.add(90, "death", location="Z", actor="Bandit", target="Hero", data={})
        self.add(120, "death", location="Z", actor="A", target="B", data={})
        rep = self.activity.kill_log(self.conn, 0, "Hero")
        self.assertEqual((rep["seen"], rep["my_kills"], rep["my_deaths"]), (4, 1, 2))
        self.assertEqual(rep["top_killers"], [{"player": "Bandit", "count": 2}])
        first = rep["rows"][-1]
        self.assertEqual((first["victim_guild"], first["killer_guild"], first["mine"]), ("G1", "G2", True))
        self.assertFalse(rep["rows"][0]["mine"])

    def test_zone_report(self):
        self.add(0, "zone", location="D1", target="D1")
        self.add(600, "fame", location="D1", value=3000)
        self.add(1200, "silver", location="D1", value=1000)
        self.add(1800, "loot", location="D1", item_id="T4_BAG", amount=2, actor="Hero", data={"silver": False})
        self.add(1810, "loot", location="D1", item_id="T4_BAG", amount=5, actor="Other", data={"silver": False})
        self.add(1820, "loot", location="D1", amount=500, actor="Hero", data={"silver": True})
        self.add(1830, "death", location="D1", actor="Mob", target="Hero", data={})
        self.add(9000, "zone", location="C", target="C")      # простой больше 10 мин не засчитывается
        self.add(9060, "fame", location="C", value=10)
        self.add(9100, "zone", location="D1", target="D1")    # второй визит
        self.add(9160, "fame", location="D1", value=100)
        rows = {r["location"]: r for r in self.activity.zone_report(self.conn, 0, "Hero", lambda i: 1000)}
        d = rows["D1"]
        self.assertEqual((d["visits"], d["fame"], d["silver"], d["loot_value"], d["loot_silver"], d["deaths"]),
                         (2, 3100, 1000, 2000, 500, 1))
        # 1830 с непрерывной игры + 600 с (обрезанная пауза до смены зоны) + 60 с второго визита.
        self.assertAlmostEqual(d["hours"], round((1830 + 600 + 60) / 3600, 2))
        self.assertEqual(d["income"], 1000 + 500 + 2000)
        self.assertEqual(d["income_per_hour"], round(3500 / ((1830 + 600 + 60) / 3600)))
        self.assertEqual(rows["C"]["visits"], 1)

    def test_api(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            parser = photon.PhotonParser(app.albion.on_request, app.albion.on_response, app.albion.on_event)
            parser.receive_packet(pb.packet(pb.response(2, {2: "Hero", 8: "3008"})))
            parser.receive_packet(event(165, {2: "Hero", 3: "MyGuild", 10: "Killer", 11: "Their"}))
            parser.receive_packet(event(82, {2: 500 * FP}))
            k = app.api_kills({})
            self.assertEqual((k["my_deaths"], k["rows"][0]["killer_guild"], k["rows"][0]["zone"]), (1, "Their", "Мартлок"))
            z = app.api_zones({})
            self.assertEqual(z["rows"][0]["name"], "Мартлок")
            self.assertEqual(z["rows"][0]["fame"], 500)


class RememberCharacterTest(unittest.TestCase):
    def test_last_character_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False)
            app = App(cfg)
            parser = photon.PhotonParser(app.albion.on_request, app.albion.on_response, app.albion.on_event)
            parser.receive_packet(pb.packet(pb.response(2, {2: "Hero", 8: "3008"})))
            app2 = App(cfg)                       # перезапуск: Join ещё не приходил
            self.assertEqual(app2.albion.character_name, "")
            self.assertEqual(app2.character(), "Hero")
            self.assertEqual(app2.api_kills({})["character"], "Hero")
            self.assertEqual(app2.zone_name("3003"), "Карлеон")


class FameDetailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.clock = [1000.0]
        self.app.activity.clock = lambda: self.clock[0]
        self.parser = photon.PhotonParser(self.app.albion.on_request, self.app.albion.on_response,
                                          self.app.albion.on_event)
        self.parser.receive_packet(pb.packet(pb.response(2, {0: 77, 2: "Hero", 8: "3008"})))

    def tearDown(self):
        self.tmp.cleanup()

    def fames(self):
        with self.app.conn() as c:
            return [(r["value"], json.loads(r["data"])) for r in
                    c.execute("SELECT value, data FROM activity_events WHERE kind = 'fame' ORDER BY id")]

    def test_formula(self):
        # база 1000, премиум (+500), сумка прозрения 200, бонус-фактор 0.1 → (1000+500+200)*1.1
        self.parser.receive_packet(event(82, {1: 5 * FP, 2: 1000 * FP, 5: True, 10: 200 * FP, 17: 0.1}))
        value, data = self.fames()[-1]
        self.assertAlmostEqual(value, 1870, places=0)
        self.assertEqual((data["base"], data["premium"], data["satchel"], data["src"]), (1000, 500, 200, "combat"))
        self.parser.receive_packet(event(82, {2: 100 * FP, 17: 1e9, 5: 1}))   # мусор в бонусе/премиуме игнорируется
        self.assertEqual(self.fames()[-1][0], 100)

    def test_source_from_finish_events(self):
        self.parser.receive_packet(event(61, {0: 77}))                  # сбор завершён (свой)
        self.clock[0] += 1
        self.parser.receive_packet(event(82, {2: 50 * FP}))
        self.clock[0] += 10
        self.parser.receive_packet(event(82, {2: 60 * FP}))             # давно — бой
        self.parser.receive_packet(event(71, {0: 999}))                 # крафт чужого персонажа — не считаем
        self.clock[0] += 10
        self.parser.receive_packet(event(82, {2: 70 * FP}))             # слава раньше события…
        self.clock[0] += 1
        self.parser.receive_packet(event(71, {0: 77}))                  # …крафта: уточняем задним числом
        self.clock[0] += 10
        self.parser.receive_packet(event(358, {}))                      # рыбалка без id — своя
        self.parser.receive_packet(event(82, {2: 80 * FP}))
        self.assertEqual([d["src"] for _, d in self.fames()], ["gathering", "combat", "crafting", "fishing"])
