import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from albion_trader import killboard as kb


def item(t, q=2):
    return {"Type": t, "Count": 1, "Quality": q, "ActiveSpells": [], "PassiveSpells": []}


def player(name, weapon, armor="T8_ARMOR_CLOTH_SET1", ip=1300.5, guild="G"):
    return {"Name": name, "GuildName": guild, "AverageItemPower": ip, "Equipment": {
        "MainHand": item(weapon), "OffHand": None, "Head": item("T8_HEAD_CLOTH_SET1"), "Armor": item(armor),
        "Shoes": item("T8_SHOES_CLOTH_SET1"), "Bag": item("T8_BAG"), "Cape": item("T8_CAPE"),
        "Mount": item("T8_MOUNT_HORSE"), "Potion": item("T6_POTION_HEAL", 0), "Food": None}, "Inventory": []}


def event(eid, killer, victim, ts="2026-09-29T10:00:00.1234567Z", participants=1):
    return {"EventId": eid, "TimeStamp": ts, "Killer": killer, "Victim": victim, "TotalVictimKillFame": 5000,
            "numberOfParticipants": participants, "Participants": [killer] * participants, "Type": "KILL"}


def mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    kb.init(c)
    return c


class ParseTest(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(kb.parse_time("2026-09-29T10:00:00Z"), 1790676000)
        self.assertEqual(kb.parse_time("2026-09-29T10:00:00.1234567Z"), 1790676000)
        for bad in (None, "", "yesterday", 5):
            self.assertIsNone(kb.parse_time(bad))

    def test_parse_event(self):
        e = kb.parse_event(event(7, player("A", "T8_2H_NATURESTAFF@2"), player("B", "T8_MAIN_SWORD")), "europe")
        self.assertEqual((e["event_id"], e["participants"], e["fame"]), (7, 1, 5000))
        self.assertEqual(e["killer"]["eq"]["mainhand"], ["T8_2H_NATURESTAFF@2", 2])
        self.assertNotIn("offhand", e["killer"]["eq"])
        self.assertNotIn("food", e["killer"]["eq"])
        for bad in (None, [], {"EventId": "x"}, {"EventId": 1, "TimeStamp": "2026-09-29T10:00:00Z"},
                    event(1, None, player("B", "X")), event(1, player("A", "X"), player("B", "X"), ts="bad")):
            self.assertIsNone(kb.parse_event(bad, "europe"))

    def test_base_item(self):
        self.assertEqual(kb.base_item("T8_2H_NATURESTAFF@3"), "2H_NATURESTAFF")
        self.assertEqual(kb.base_item("UNIQUE_MOUNT_X"), "UNIQUE_MOUNT_X")


class MetaTest(unittest.TestCase):
    def setUp(self):
        self.c = mem()
        staff, sword = "T8_2H_NATURESTAFF@1", "T7_MAIN_SWORD"
        evs = [event(1, player("A", staff), player("B", sword)),
               event(2, player("C", "T6_2H_NATURESTAFF"), player("D", sword), ts="2026-09-29T11:00:00Z"),
               event(3, player("E", sword), player("F", staff, ip=800), participants=5),
               event(4, player("G", "T8_2H_BOW", armor="T8_ARMOR_LEATHER_SET1"), player("H", sword))]
        kb.store(self.c, [kb.parse_event(e, "europe") for e in evs])

    def test_dedup_and_cleanup(self):
        again = kb.parse_event(event(1, player("A", "X"), player("B", "Y")), "europe")
        self.assertEqual(kb.store(self.c, [again]), 0)
        self.assertEqual(kb.cleanup(self.c, now=1790676000 + 8 * 86400), 4)

    def test_builds_aggregated_without_tiers(self):
        r = kb.meta_report(self.c, "europe", 0, min_count=1)
        self.assertEqual(r["events"], 4)
        by = {b["signature"]["mainhand"]: b for b in r["builds"]}
        staff = by["2H_NATURESTAFF"]
        self.assertEqual((staff["kills"], staff["deaths"], staff["win_rate"]), (2, 1, 66.7))
        self.assertEqual(staff["example"]["mainhand"]["item"], "T6_2H_NATURESTAFF")   # самое свежее убийство
        self.assertEqual((by["MAIN_SWORD"]["kills"], by["MAIN_SWORD"]["deaths"]), (1, 3))
        self.assertEqual(r["builds"][0]["signature"]["mainhand"], "MAIN_SWORD")         # чаще всего встречается
        self.assertEqual(r["popular"]["mainhand"][0], {"base": "MAIN_SWORD", "count": 4})

    def test_filters_and_demand(self):
        r = kb.meta_report(self.c, "europe", 0, min_ip=1000, min_count=1)
        staff = {b["signature"]["mainhand"]: b for b in r["builds"]}["2H_NATURESTAFF"]
        self.assertEqual(staff["deaths"], 0)                     # смерть с силой 800 отфильтрована
        self.assertEqual(kb.meta_report(self.c, "europe", 0, mode="group", min_count=1)["events"], 1)
        self.assertEqual(kb.meta_report(self.c, "europe", 0, mode="solo", min_count=1)["events"], 3)
        self.assertEqual(kb.meta_report(self.c, "asia", 0)["events"], 0)
        prices = {"T7_MAIN_SWORD": 100.0}
        r = kb.meta_report(self.c, "europe", 0, value_of=prices.get)
        sword = [d for d in r["demand"] if d["item_id"] == "T7_MAIN_SWORD"][0]
        self.assertEqual((sword["lost"], sword["turnover"]), (3, 300))
        self.assertIsNone([d for d in r["demand"] if d["item_id"] == "T8_CAPE"][0]["turnover"])
        # min_count по умолчанию 2: одиночный лук не попадает в билды
        self.assertNotIn("2H_BOW", {b["signature"]["mainhand"] for b in r["builds"]})


class FetcherTest(unittest.TestCase):
    def make(self, pages, fail=False):
        c = mem()
        lock = threading.Lock()
        from contextlib import contextmanager

        @contextmanager
        def conn():
            yield c
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req.full_url)
            if fail:
                raise urllib.error.URLError("нет сети")
            offset = int(req.full_url.rsplit("offset=", 1)[1])
            return io.BytesIO(json.dumps(pages.get(offset, [])).encode())
        f = kb.KillboardFetcher(conn, lock, lambda: {}, urlopen=urlopen, sleep=lambda s: None,
                                clock=lambda: 1790676000)
        return f, c, calls

    def page(self, start, n):
        return [event(i, player("A", "T8_MAIN_SWORD"), player("B", "T8_MAIN_SWORD")) for i in range(start, start + n)]

    def test_stops_at_known_events(self):
        pages = {0: self.page(100, 51), 51: self.page(49, 51), 102: self.page(0, 51)}
        f, c, calls = self.make(pages)
        self.assertEqual(f.fetch_once("europe"), 51 + 51 + 49)   # на третьей странице два уже известных — стоп
        self.assertEqual(len(calls), 3)
        self.assertIn("gameinfo-ams.albiononline.com/api/gameinfo/events?limit=51&offset=0", calls[0])
        self.assertEqual(f.fetch_once("europe"), 0)          # всё уже есть — одна страница
        self.assertEqual(len(calls), 4)
        self.assertEqual(f.status["last_new"], 0)

    def test_errors(self):
        f, c, calls = self.make({}, fail=True)
        with self.assertRaises(OSError):
            f.fetch_once("europe")
        self.assertIn("нет сети", f.status["error"])
        self.assertFalse(f.status["running"])
        with self.assertRaises(ValueError):
            f.fetch_once("mars")


class KillboardApiTest(unittest.TestCase):
    def setUp(self):
        from albion_trader.server import App, AppConfig, make_handler
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "items.json").write_text(json.dumps({"names": {"T8_MAIN_SWORD": {"ru": "Меч (старейшина)", "en": "Elder's Broadsword"}}, "index": {}}))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", capture=False))
        with self.app.conn() as c:
            now = __import__("time").time()
            from datetime import datetime, timezone
            ts = datetime.fromtimestamp(now - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            kb.store(c, [kb.parse_event(event(i, player("A", "T8_MAIN_SWORD"), player("B", "T8_MAIN_SWORD"), ts=ts),
                                        "europe") for i in range(3)])
        self.woke = []
        self.app.killboard.fetch_async = lambda: self.woke.append(1)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, body):
        req = urllib.request.Request(self.base + "/api/killboard", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_report_and_settings(self):
        with urllib.request.urlopen(self.base + "/api/killboard?hours=24") as r:
            data = json.load(r)
        self.assertEqual((data["stored"], data["enabled"], data["region"]), (3, False, "europe"))
        self.assertEqual(data["builds"][0]["names"]["mainhand"], "Меч")
        self.assertEqual(data["builds"][0]["kills"], 3)
        self.assertEqual(self.post({"enabled": True, "region": "asia"})["region"], "asia")
        self.assertEqual(self.woke, [1])                        # включили — сразу загрузка
        self.post({"action": "fetch"})
        self.assertEqual(len(self.woke), 2)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({"region": "mars"})
        self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/api/killboard?region=mars")
        with urllib.request.urlopen(self.base + "/api/killboard?hours=nan&min_ip=inf&mode=x") as r:
            self.assertEqual(json.load(r)["region"], "asia")


if __name__ == "__main__":
    unittest.main()
