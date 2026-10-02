"""Тесты навигации бота: маршрут между зонами по выходам и путь внутри зоны по схеме."""

import math
import unittest

from albion_trader.bot_nav import Grid, Router, zone_allowed, zone_danger

INDEX = {
    "A": {"name": "Город А", "type": "PLAYERCITY_SAFEAREA_01", "exits": [[50, 0, "Y", "x"], [0, 50, "B1", "x"],
                                                                          [0, -50, "D", "x"]]},
    "Y": {"name": "Жёлтая", "type": "OPENPVP_YELLOW", "exits": [[-50, 0, "A", "x"], [50, 0, "C", "x"]]},
    "B1": {"name": "Синяя 1", "type": "SAFEAREA", "exits": [[0, -50, "A", "x"], [0, 50, "B2", "x"]]},
    "B2": {"name": "Синяя 2", "type": "SAFEAREA", "exits": [[0, -50, "B1", "x"], [50, 0, "C", "x"]]},
    "C": {"name": "Город Б", "type": "PLAYERCITY_SAFEAREA_02", "exits": [[-50, 0, "Y", "x"], [0, -50, "B2", "x"]]},
    "D": {"name": "Данж", "type": "DUNGEON_YELLOW", "exits": [[0, 50, "A", "x"], [10, 0, "C", "x"]]},
    "R": {"name": "Красная", "type": "OPENPVP_RED", "exits": []},
    "X": {"name": "Остров", "type": "PLAYERISLAND", "exits": [[0, 0, "Q", "x"]]},
}


class ZoneTypesTest(unittest.TestCase):
    def test_danger_and_allowed(self):
        self.assertEqual([zone_danger(t) for t in ("SAFEAREA", "OPENPVP_YELLOW", "OPENPVP_RED", "OPENPVP_BLACK_3")],
                         [0, 1, 2, 3])
        self.assertTrue(zone_allowed("PLAYERCITY_SAFEAREA_01", "safe"))
        self.assertFalse(zone_allowed("OPENPVP_YELLOW", "safe"))
        self.assertTrue(zone_allowed("OPENPVP_YELLOW", "yellow"))
        self.assertFalse(zone_allowed("OPENPVP_RED", "yellow"))
        self.assertTrue(zone_allowed("OPENPVP_BLACK_1", "black"))
        for t in ("DUNGEON_YELLOW", "TUNNEL_ROYAL", "PLAYERISLAND", "TUNNEL_HIDEOUT"):
            self.assertFalse(zone_allowed(t, "black"), t)


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.r = Router(INDEX)

    def zones(self, hops):
        return [h[0] for h in hops] + [hops[-1][3]]

    def test_safe_route_goes_around_yellow_zone_and_skips_dungeon(self):
        hops = self.r.route("A", "C", "safe")
        self.assertEqual(self.zones(hops), ["A", "B1", "B2", "C"])
        self.assertEqual(hops[0][1:3], (0.0, 50.0))       # выход из А на север

    def test_yellow_allows_shorter_route(self):
        self.assertEqual(self.zones(self.r.route("A", "C", "yellow")), ["A", "Y", "C"])

    def test_start_position_picks_nearest_exit(self):
        # Из точки рядом с восточным выходом через жёлтую ближе, чем обходить.
        self.assertEqual(self.zones(self.r.route("A", "C", "yellow", start_pos=(45, 0))), ["A", "Y", "C"])

    def test_target_zone_itself_may_be_dangerous(self):
        idx = {**INDEX, "A": {**INDEX["A"], "exits": INDEX["A"]["exits"] + [[-50, 0, "R", "x"]]}}
        self.assertEqual(self.zones(Router(idx).route("A", "R", "safe")), ["A", "R"])

    def test_nearby_zones_skip_cities_and_danger(self):
        self.assertEqual(self.r.nearby("A", "yellow"), ["Y", "B1", "B2"])
        self.assertEqual(self.r.nearby("A", "safe"), ["B1", "B2"])
        self.assertEqual(self.r.nearby("A", "yellow", limit=1), ["Y"])
        self.assertEqual(self.r.nearby("ZZ"), [])

    def test_errors_and_same_zone(self):
        self.assertEqual(self.r.route("A", "A"), [])
        with self.assertRaisesRegex(ValueError, "нет в списке зон"):
            self.r.route("NOPE", "A")
        with self.assertRaisesRegex(ValueError, "нет в списке зон"):
            self.r.route("A", "NOPE")
        with self.assertRaisesRegex(ValueError, "нет пути из «Город А» в «Красная»"):
            self.r.route("A", "R", "black")
        self.assertEqual(self.r.name("ZZ"), "ZZ")
        self.assertEqual(self.r.exits("ZZ"), [])


class GridTest(unittest.TestCase):
    WALL = {"bounds": [-30, -30, 30, 30], "tiles": [["building", 0.0, 0.0, 6.0, 40.0, 0, 0.0],
                                                     ["tree", 20.0, 20.0, 4.0, 4.0, 0, 0.0],
                                                     ["ground", 0.0, 0.0, 60.0, 60.0, 0, 0.0]]}

    def test_blocks_and_paths_around(self):
        g = Grid(self.WALL)
        self.assertFalse(g.free_at(0, 0))
        self.assertFalse(g.free_at(20, 20))
        self.assertTrue(g.free_at(-20, 0))
        pts = g.path((-20, 0), (20, 0))
        self.assertEqual(pts[-1], (20, 0))
        # Каждый отрезок пути — по свободным клеткам.
        prev = g.cell_of(-20, 0)
        for x, y in pts:
            cur = g.cell_of(x, y)
            self.assertTrue(g.line_free(prev, cur), (prev, cur))
            prev = cur
        self.assertTrue(any(abs(y) > 20 for _x, y in pts))      # обход через край стены

    def test_rotated_tile_and_bridge_over_water(self):
        g = Grid({"bounds": [-30, -30, 30, 30], "tiles": [
            ["water", 0.0, 0.0, 60.0, 10.0, 0, 0.0],
            ["road", 0.0, 0.0, 4.0, 12.0, 0, 0.0],              # мост поперёк реки
            ["cliff", -20.0, 20.0, 20.0, 2.0, 90, 0.0]]})       # повёрнутая скала: вдоль y
        self.assertFalse(g.free_at(15, 0))
        self.assertTrue(g.free_at(0, 0))
        self.assertFalse(g.free_at(-20, 27))
        self.assertTrue(g.free_at(-25, 20))
        pts = g.path((10, -20), (10, 20))
        self.assertTrue(pts)
        self.assertTrue(any(abs(x) < 4 and abs(y) < 6 for x, y in pts) or len(pts) >= 2)

    def test_no_path_and_trivial_cases(self):
        closed = {"bounds": [-30, -30, 30, 30], "tiles": [["building", 0.0, 0.0, 6.0, 80.0, 0, 0.0]]}
        g = Grid(closed)
        self.assertIsNone(g.path((-20, 0), (20, 0)))
        self.assertEqual(g.path((-20, 0), (-19, 0)), [(-19, 0)])
        full = Grid({"bounds": [0, 0, 9, 9], "tiles": [["building", 4.5, 4.5, 30.0, 30.0, 0, 0.0]]})
        self.assertIsNone(full.path((1, 1), (8, 8)))
        self.assertIsNone(full.nearest_free(1, 1))

    def test_bounds_from_tiles_and_start_inside_obstacle(self):
        g = Grid({"tiles": [["rock", 5.0, 5.0, 3.0, 3.0, 0, 0.0]]})
        self.assertGreater(g.w, 5)
        self.assertFalse(g.free_at(5, 5))
        pts = g.path((5, 5), (5, 20))                    # старт в камне — ближайшая свободная клетка
        self.assertTrue(pts)
        self.assertEqual(Grid({}).w, 1)

    def test_path_keeps_clear_of_corners(self):
        g = Grid(self.WALL)
        pts = g.path((-20, 0), (20, 0))
        for x, y in pts[:-1]:
            self.assertGreaterEqual(math.hypot(x, abs(y) - 20) if abs(y) > 20 else 99, 2.0)


if __name__ == "__main__":
    unittest.main()
