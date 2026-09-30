import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from albion_trader import avalon
from albion_trader.gamedata import GameData

CLUSTERS = {
    "4206": ["Tharcal Fissure", "OPENPVP_YELLOW", ["4208"]],
    "4208": ["Pen Garn", "OPENPVP_RED", ["4206", "4300"]],
    "4300": ["Deep Wood", "OPENPVP_BLACK_1", ["4208"]],
    "TNL-001": ["Ouyos-Aoeuam", "TUNNEL_ROYAL", []],
    "TNL-002": ["Coues-Exakrom", "TUNNEL_BLACK_LOW", []],
    "TNL-003": ["Puros-Amayam", "TUNNEL_HIDEOUT", []],
}


def gd():
    return GameData({"items": {}, "recipes": {}, "clusters": CLUSTERS})


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    avalon.init(c)
    return c


class LogicTest(unittest.TestCase):
    def test_resolve_and_search(self):
        g = gd()
        self.assertEqual(avalon.resolve_zone(g, "ouyos-aoeuam"), "TNL-001")
        self.assertEqual(avalon.resolve_zone(g, "4206"), "4206")
        self.assertEqual(avalon.resolve_zone(g, "TNL-777"), "TNL-777")       # нет в справочнике, но формат Дороги
        self.assertIsNone(avalon.resolve_zone(g, "Нарния"))
        self.assertEqual([z["id"] for z in avalon.search_zones(g, "o")][:1], ["TNL-002"])   # сначала Дороги
        self.assertTrue(avalon.is_road(g, "TNL-777"))
        self.assertFalse(avalon.is_road(g, "4206"))

    def test_add_update_cleanup(self):
        c = conn()
        lid = avalon.add_link(c, "TNL-002", "TNL-001", 7, 2, "у входа", now=1000)
        row = avalon.links(c, now=1000)[0]
        self.assertEqual((row["a"], row["b"], row["size"], row["left"]), ("TNL-001", "TNL-002", 7, 7200))
        # автопереход по той же связи не затирает ручное время
        self.assertEqual(avalon.add_link(c, "TNL-001", "TNL-002", source="auto", now=2000), lid)
        self.assertEqual(avalon.links(c, now=2000)[0]["expires"], 1000 + 7200)
        avalon.update_link(c, lid, 20, 0, "", now=3000)
        self.assertIsNone(avalon.links(c, now=3000)[0]["expires"])
        for bad in (("A", "A"), ("", "B")):
            with self.assertRaises(ValueError):
                avalon.add_link(c, *bad)
        with self.assertRaises(ValueError):
            avalon.add_link(c, "A", "B", size=5)
        with self.assertRaises(ValueError):
            avalon.update_link(c, 999, 2, 1, "")
        avalon.add_link(c, "TNL-003", "4300", 2, 1, now=1000)              # истекает в 4600
        avalon.add_link(c, "TNL-003", "TNL-002", source="auto", now=1000)  # без времени — сутки
        self.assertEqual(avalon.cleanup(c, now=5000), 1)
        self.assertEqual(avalon.cleanup(c, now=1000 + avalon.AUTO_TTL + 1), 1)
        self.assertEqual(len(avalon.links(c)), 1)                          # ручная без времени остаётся

    def test_auto_detection(self):
        c, g = conn(), gd()
        self.assertIsNone(avalon.on_zone_change(c, g, "4208", "4206"))           # обычный переход карты
        self.assertIsNone(avalon.on_zone_change(c, g, "4208", ""))
        self.assertIsNotNone(avalon.on_zone_change(c, g, "TNL-001", "4206"))     # вошли в Дорогу
        self.assertIsNotNone(avalon.on_zone_change(c, g, "TNL-002", "TNL-001"))
        self.assertEqual({(l["a"], l["b"], l["source"]) for l in avalon.links(c)},
                         {("4206", "TNL-001", "auto"), ("TNL-001", "TNL-002", "auto")})

    def test_route(self):
        c, g = conn(), gd()
        avalon.add_link(c, "4206", "TNL-001", 7, 3, now=1000)
        avalon.add_link(c, "TNL-001", "TNL-002", 20, 1, now=1000)
        avalon.add_link(c, "TNL-002", "4300", 20, None, now=1000)
        rows = avalon.links(c, now=1000)
        r = avalon.route(g, rows, "4206", "4300", use_static=False, now=1000)
        self.assertEqual(r["zones"], ["4206", "TNL-001", "TNL-002", "4300"])
        self.assertEqual((r["portals"], r["expires"]), (3, 1000 + 3600))
        # обычные переходы короче: 4206 → 4208 → 4300
        r = avalon.route(g, rows, "4206", "4300", use_static=True, now=1000)
        self.assertEqual((r["zones"], r["portals"]), (["4206", "4208", "4300"], 0))
        # истёкший портал не используется
        self.assertIsNone(avalon.route(g, rows, "4206", "TNL-002", use_static=False, now=1000 + 7200))
        self.assertEqual(avalon.route(g, rows, "4206", "4206")["zones"], ["4206"])


class AvalonApiTest(unittest.TestCase):
    def setUp(self):
        from albion_trader.server import App, AppConfig, make_handler
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "gamedata.json").write_text(json.dumps({"version": 2, "items": {}, "recipes": {}, "clusters": CLUSTERS}))
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", capture=False))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, body):
        req = urllib.request.Request(self.base + "/api/avalon", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def test_flow_with_capture(self):
        # Переход через портал из захвата: Join в Tharcal Fissure, затем в Дорогу.
        from albion_trader.capture import photon
        try:
            from . import photon_builder as pb
        except ImportError:
            import photon_builder as pb
        parser = photon.PhotonParser(self.app.albion.on_request, self.app.albion.on_response, self.app.albion.on_event)
        parser.receive_packet(pb.packet(pb.response(2, {0: 1, 2: "Hero", 8: "4206"})))
        parser.receive_packet(pb.packet(pb.response(2, {0: 2, 2: "Hero", 8: "TNL-001"})))
        data = self.get("/api/avalon")
        self.assertEqual(data["zone"]["name"], "Ouyos-Aoeuam")
        self.assertEqual([(l["a"], l["b"]) for l in data["links"]], [("4206", "TNL-001")])
        res = self.post({"action": "add", "a": "Ouyos-Aoeuam", "b": "Coues-Exakrom", "size": 20, "hours": 1, "minutes": 30})
        link = [l for l in res["links"] if l["b"] == "TNL-002"][0]
        self.assertEqual(link["size"], 20)
        self.assertIn(link["left"] // 60, (89, 90))
        route = self.get("/api/avalon?from=4206&to=TNL-002")["route"]
        self.assertEqual([z["id"] for z in route["zones"]], ["4206", "TNL-001", "TNL-002"])
        self.assertIn("route_error", self.get("/api/avalon?from=4206&to=4300&static=0"))
        self.assertIn("не нашёл", self.get("/api/avalon?from=4206&to=Nowhere")["route_error"])
        self.assertEqual(self.get("/api/avalon?q=coues")["zones"][0]["id"], "TNL-002")
        res = self.post({"action": "update", "id": link["id"], "size": 7, "hours": 0})
        self.assertIsNone([l for l in res["links"] if l["id"] == link["id"]][0]["expires"])
        res = self.post({"action": "delete", "id": link["id"]})
        self.assertEqual(len(res["links"]), 1)
        for bad in ({"action": "add", "a": "Nowhere", "b": "4206"}, {"action": "add", "a": "4206", "b": "4206"},
                    {"action": "add", "a": "4206", "b": "4208", "size": 3}, {"action": "delete", "id": "x"},
                    {"action": "nope"}):
            with self.assertRaises(urllib.error.HTTPError, msg=bad) as cm:
                self.post(bad)
            self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
