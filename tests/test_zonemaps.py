import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

from albion_trader import window
from albion_trader.server import App, AppConfig
from albion_trader.zonemaps import (ZONEMAP_VERSION, ZoneMaps, classify, parse_cluster, parse_template,
                                    parse_world_index)

WORLD = {"world": {"clusters": {"cluster": [
    {"@id": "0201", "@file": "0201_WRL.cluster.xml", "@type": "OPENPVP_RED", "@displayname": "Sleetwater",
     "exits": {"exit": [{"@pos": "80.5 380.5", "@targetid": "abc@4220", "@minimapicon": "ClusterExit"}]}},
    {"@id": "4220", "@file": "4220_WRL.cluster.xml", "@type": "SAFEAREA", "@displayname": "Neighbour"},
]}}}

CLUSTER = """<cluster minimapBoundsMin="-415 -415" minimapBoundsMax="415 415" minimapHeightRange="0 16">
  <templateinstance id="a" ref="BASE" pos="0 0 0"><activelayer id="on" /></templateinstance>
  <templateinstance id="b" ref="EXIT" pos="360 0 -70" rot="90"><activelayer id="ex" /></templateinstance>
</cluster>"""

BASE = """<template editorBoundsMin="-465 -465" editorBoundsMax="465 465">
  <tiles>
    <layergroup name="Main">
      <layer id="on" name="route_on">
        <tile name="SWAMP_RED_WATER_river_OBJ" pos="10 -2 20" />
        <compoundtile name="_SWAMP_RED_ROAD_STRAIGHT_A_COMP" pos="30 0 0" roty="90" />
      </layer>
      <layer id="off" name="route_off">
        <tile name="SWAMP_RED_WATER_river_OBJ" pos="-10 0 -20" />
      </layer>
      <layer id="Layer_09" name="Halloween" flags="halloween">
        <tile name="SWAMP_RED_GROUND_20x" pos="0 0 0" />
      </layer>
      <layer id="inc" name="included">
        <tile name="SWAMP_RED_GROUND_20x20" pos="5 8 5" />
        <tile name="SWAMP_RED_VEG_GRASS_A" pos="1 0 1" />
      </layer>
    </layergroup>
  </tiles>
</template>"""

EXIT = """<template editorBoundsMin="-105 -45" editorBoundsMax="105 45">
  <criticalscenemembers>
    <criticalscenemember id="exit_05" type="exit" layerId="ex">
      <tile name="Exit" pos="10.5 0 20.5" roty="90"><exit minimapicon="ClusterExit" /></tile>
    </criticalscenemember>
    <criticalscenemember id="exit_01" type="exit" layerId="other">
      <tile name="Exit" pos="0 0 0"><exit minimapicon="ClusterExit" /></tile>
    </criticalscenemember>
  </criticalscenemembers>
  <tiles><layergroup name="Main"><layer id="ex" name="exit">
    <tile name="SWAMP_RED_PLATEAU_WALL_STRAIGHT_20M_A" pos="0 0 10" />
  </layer></layergroup></tiles>
</template>"""


class FakeDumps:
    def __init__(self):
        self.files = {"cluster/world.json": json.dumps(WORLD).encode(),
                      "cluster/0201_WRL.cluster.xml": CLUSTER.encode(),
                      "templates/GREEN/BASE.template.xml": BASE.encode(),      # только в GREEN
                      "templates/RED/EXIT.template.xml": EXIT.encode()}
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path not in self.files:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return self.files[path]


class ClassifyTest(unittest.TestCase):
    def test_biome_prefix_is_not_a_category(self):
        self.assertEqual(classify("ROADS_GROUND")[0], "ground")
        self.assertEqual(classify("FOREST_RED_GROUND_80x80_OBJ_NOCLICK"), ("ground", 80.0, 80.0))
        self.assertEqual(classify("SWAMP_RED_WATER_river_OBJ")[0], "water")
        self.assertEqual(classify("_SWAMP_RED_ROAD_STRAIGHT_A_COMP")[0], "road")
        self.assertEqual(classify("HIGHLAND_GREEN_PLATEAU_WALL_STRAIGHT_A")[0], "cliff")
        self.assertEqual(classify("HIGHLAND_GREEN_CONSTRUCTION_CORNER")[0], "plot")
        self.assertEqual(classify("HIGHLAND_GREEN_MARKETPLACE")[0], "building")
        self.assertEqual(classify("HIGHLAND_GREEN_T1_TREE_DOMESTIC_A")[0], "tree")
        self.assertEqual(classify("_SWAMP_RED_10x20_B"), ("ground", 10.0, 20.0))
        self.assertIsNone(classify("HIGHLAND_GREEN_VEG_GRASS_B"))
        self.assertIsNone(classify("XMAS_DECO_SMALL_A"))
        self.assertEqual(classify("SWAMP_RED_PLATEAU_WALL_STRAIGHT_20M_A")[1:], (20.0, 4.0))


class ParseTest(unittest.TestCase):
    def test_template_layers_and_flags(self):
        t = parse_template(BASE)
        layers = {t["layers"][i] for *_, i in t["tiles"]}
        self.assertEqual(layers, {"on", "off", "inc"})           # праздничный слой отброшен
        self.assertEqual(t["included"], [t["layers"].index("inc")])
        self.assertEqual(len(t["tiles"]), 4)                     # трава не рисуется

    def test_cluster_and_world_index(self):
        c = parse_cluster(CLUSTER)
        self.assertEqual([(i["ref"], i["rot"]) for i in c["instances"]], [("BASE", 0.0), ("EXIT", 90.0)])
        self.assertEqual(c["bounds"], [-415.0, -415.0, 415.0, 415.0])
        idx = parse_world_index(WORLD)
        self.assertEqual(idx["0201"]["exits"], [[80.5, 380.5, "4220", "ClusterExit"]])


class ZoneMapsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dumps = FakeDumps()
        self.zm = ZoneMaps(Path(self.tmp.name) / "zonemaps", fetch=self.dumps,
                           name_of=lambda cid: {"4220": "Neighbour"}.get(cid, cid))

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_rotation_layers_and_exits(self):
        m = self.zm.get("0201", background=False)
        self.assertEqual(m["status"], "ready")
        self.assertEqual((m["name"], m["version"]), ("Sleetwater", ZONEMAP_VERSION))
        tiles = {(t[0], t[1], t[2]) for t in m["tiles"]}
        self.assertIn(("water", 10.0, 20.0), tiles)
        self.assertNotIn(("water", -10.0, -20.0), tiles)        # выключенный слой
        self.assertIn(("ground", 5.0, 5.0), tiles)               # слой «included»
        # Шаблон выхода повёрнут на 90° и сдвинут: как в world.json.
        self.assertEqual(m["exits"], [[380.5, -80.5, "ClusterExit"]])
        self.assertIn(("cliff", 370.0, -70.0), tiles)
        self.assertEqual(m["exits_world"], [[80.5, 380.5, "4220", "ClusterExit", "Neighbour"]])
        # Зона красная → шаблоны ищутся сначала в RED; BASE есть только в GREEN — нашёлся после 404.
        self.assertLess(self.dumps.calls.index("templates/RED/BASE.template.xml"),
                        self.dumps.calls.index("templates/GREEN/BASE.template.xml"))

    def test_cache_and_background_loading(self):
        first = self.zm.get("0201")
        self.assertEqual(first["status"], "loading")
        for _ in range(100):
            if self.zm.get("0201")["status"] == "ready":
                break
            time.sleep(0.02)
        self.assertEqual(self.zm.get("0201")["status"], "ready")
        n = len(self.dumps.calls)
        ZoneMaps(self.zm.dir, fetch=self.dumps).get("0201")      # из кэша на диске
        self.assertEqual(len(self.dumps.calls), n)

    def test_unknown_zone_and_retry(self):
        self.assertEqual(self.zm.get("")["status"], "unknown")
        r = self.zm.get("9999", background=False)
        self.assertEqual(r["status"], "error")
        self.assertIn("9999", r["error"])
        self.zm.retry("9999")
        self.assertNotIn("9999", self.zm.errors)

    def test_prefetch(self):
        res = self.zm.prefetch(["0201", "0201", "nope"])
        self.assertEqual((res["ok"], res["failed"], res["total"]), (1, 1, 2))


class AppZoneMapTest(unittest.TestCase):
    def test_api_and_radar_window(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            dumps = FakeDumps()
            app.zonemaps.fetch = dumps
            self.assertEqual(app.api_zonemap({})["status"], "unknown")      # зона ещё неизвестна
            app.radar.on_zone("0201")
            for _ in range(100):
                m = app.api_zonemap({})
                if m["status"] != "loading":
                    break
                time.sleep(0.02)
            self.assertEqual(m["status"], "ready")
            self.assertEqual(app.radar_window.url, "http://127.0.0.1:8484/" + window.RADAR_PAGE)
            self.assertEqual(app.radar_window.title, window.RADAR_TITLE)
            self.assertNotEqual(app.radar_window.profile_dir, app.window.profile_dir)
            self.assertIn("running", app.api_window({"which": "radar"}))


if __name__ == "__main__":
    unittest.main()
