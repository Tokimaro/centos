"""Тесты бота на модели игры: клик в окне двигает персонажа, бьёт моба, открывает
сундук или переводит в другую зону, а программа видит это так же, как в жизни, —
через пакеты Photon этого окна (свой UDP-порт)."""

import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from albion_trader import bots as bots_mod
from albion_trader.bot_core import (POINTS, TEMPLATES, BotError, BotStopped, Calibration, ClientFeed, fill,
                                    market_price, parse_macro, parse_skills, solve_calibration, split_items)
from albion_trader.bot_tasks import DEFAULTS
from albion_trader.bot_win import VK, VK_LBUTTON, GameWindow
from albion_trader.bots import BotManager, Recorder
from albion_trader.capture.albion import DEFAULT_EVENTS, DEFAULT_OPCODES
from albion_trader.radar import Radar

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb

TRUE_M = [[14.0, 20.0], [16.0, -23.0]]     # «настоящая» камера модели (отличается от умолчания)
ALL_POINTS = {name: [round(0.05 + 0.06 * i, 3), round(0.1 + 0.055 * i, 3)] for i, name in enumerate(POINTS)}

# Модель мира: город А — дорога — город Б; выходы на ±50 м по x.
INDEX = {
    "CITYA": {"name": "Город А", "type": "PLAYERCITY_SAFEAREA_01", "exits": [[50, 0, "ROAD", "ClusterExit"]]},
    "ROAD": {"name": "Дорога", "type": "OPENPVP_YELLOW",
             "exits": [[-50, 0, "CITYA", "ClusterExit"], [50, 0, "CITYB", "ClusterExit"]]},
    "CITYB": {"name": "Город Б", "type": "PLAYERCITY_SAFEAREA_02", "exits": [[-50, 0, "ROAD", "ClusterExit"]]},
}


class FakeZonemaps:
    def __init__(self, maps=None, index=None):
        self.maps = maps or {}
        self.idx = index or INDEX

    def index(self):
        return self.idx

    def get(self, zone, background=True):
        m = self.maps.get(zone)
        return {"status": "ready", **m} if m else {"status": "error"}

    def zone_name(self, z):
        return (self.idx.get(z) or {}).get("name") or z


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.limit = None
        self.on_sleep = []

    def __call__(self):
        return self.t

    def sleep(self, seconds, event):
        self.t += seconds
        for fn in self.on_sleep:
            fn()
        if self.limit is not None and self.t > self.limit:
            event.set()
        return event.is_set()


class FakeDesktop:
    available = True

    def __init__(self, game):
        self.game = game
        self.clicks, self.typed, self.keys, self.focused, self.moved_cursor = [], [], [], [], []
        self.fg = 1
        self.pressed = set()
        self.cur = (0, 0)
        self.idle = 1e9
        self.alive = True
        self.focus_ok = True

    def game_windows(self):
        return list(self.game.windows.values())

    def window_alive(self, hwnd):
        return self.alive

    def idle_seconds(self):
        return self.idle

    def key_down(self, vk):
        return vk in self.pressed

    def foreground(self):
        return self.fg

    def cursor(self):
        return self.cur

    def focus(self, hwnd):
        self.focused.append(hwnd)
        if self.focus_ok:
            self.fg = hwnd
        return self.focus_ok

    def move_cursor(self, x, y):
        self.moved_cursor.append((x, y))

    def sleep(self, _s):
        pass

    def click(self, win, fx, fy, button="left", background=False):
        self.clicks.append((win.pid, round(fx, 3), round(fy, 3), button, background))
        self.game.click(win.pid, fx, fy, button)

    def type_text(self, win, text, background=False):
        self.typed.append((win.pid, text))
        self.game.request(win.pid, 99, {})

    def press(self, win, combo, background=False):
        self.keys.append((win.pid, combo))
        self.game.key(win.pid, combo)


class FakeGame:
    """Модель игры: персонажи в окнах, зоны с выходами и порталами, ресурсы, мобы,
    игроки, добыча. Объекты живут в своей зоне и приходят событиями при входе в неё."""

    def __init__(self):
        self.manager = None
        self.windows, self.pos, self.port, self.zone = {}, {}, {}, {}
        self.nodes = {}           # id → (x, y)
        self.mobs = {}            # id → [x, y, hp, зона, имя]
        self.loot = {}            # id → (x, y, зона, сундук?)
        self.links = {}           # id → (x, y, зона, куда, x там, y там, событие, имя)
        self.players = {}         # id → (x, y, зона, имя, флаг)
        self.harvest_ok = True
        self.ignore_clicks = False
        self.center = (0.5, 0.5)  # где персонаж на экране на самом деле
        self.walls = []           # [(x0, y0, x1, y1)] — непроходимо
        self.attacking = None
        self.opened = None
        self.next_id = 5000
        self.my_hp = None
        self.clock = None
        self.exit_map = {}        # зона данжа → (куда выводит быстрый выход, позиция)
        self.exiting = None       # (pid, когда закончится задержка)
        self.damage_on_exit = 0   # сколько первых попыток выхода собьёт урон
        self.items_per_loot = 0   # предметов в сумку с каждой добычи
        self.items_per_harvest = 0
        self.index = INDEX

    def add_window(self, pid, name, pos=(0.0, 0.0), zone="0201", rect=(0, 0, 1600, 900)):
        port = 50000 + pid
        self.windows[pid] = GameWindow(pid * 10, pid, "Albion Online", "Albion-Online.exe", rect, [port])
        self.port[pid] = port
        self.manager.refresh()
        self.join(pid, name, zone, pos)

    def join(self, pid, name, zone, pos):
        self.pos[pid], self.zone[pid] = pos, zone
        self.attacking = self.opened = None
        self.send(pid, pb.response(DEFAULT_OPCODES["join"], {0: pid, 2: name, 8: zone,
                                                              9: [float(pos[0]), float(pos[1])]}))
        for mid, m in self.mobs.items():
            if m[3] == zone:
                self._send_mob(pid, mid)
        for lid, lt in self.loot.items():
            if lt[2] == zone:
                self._send_loot(pid, lid)
        for lid, ln in self.links.items():
            if ln[2] == zone:
                self._send_link(pid, lid)
        for cid, pl in self.players.items():
            if pl[2] == zone:
                self._send_player(pid, cid)

    def here(self, pid, zone):
        return zone == self.zone.get(pid)

    def send(self, pid, command):
        self.manager.on_packet(self.port[pid], pb.packet(command))

    def request(self, pid, op, params):
        self.send(pid, pb.request(op, params))

    def event(self, pid, name, params):
        self.send(pid, pb.event(DEFAULT_EVENTS[name], params))

    def blocked(self, x, y):
        return any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in self.walls)

    def move_to(self, pid, x, y):
        x0, y0 = self.pos[pid]
        n = max(1, int(math.hypot(x - x0, y - y0) / 0.5))
        fx, fy = x0, y0
        for i in range(1, n + 1):          # идём, пока не упрёмся в стену
            px, py = x0 + (x - x0) * i / n, y0 + (y - y0) * i / n
            if self.blocked(px, py):
                break
            fx, fy = px, py
        self.pos[pid] = (fx, fy)
        self.request(pid, DEFAULT_OPCODES["move"], {1: [float(fx), float(fy)]})
        zone = self.zone[pid]
        name = self.manager.feed_for(pid).character
        for ex, ey, target, _icon in (self.index.get(zone) or {}).get("exits", []):
            if math.hypot(fx - ex, fy - ey) < 2.0:
                back = min((e for e in self.index[target]["exits"] if e[2] == zone),
                           key=lambda e: math.hypot(e[0] - ex, e[1] - ey))
                n = math.hypot(back[0], back[1]) or 1.0
                self.join(pid, name, target, (back[0] - 4 * back[0] / n, back[1] - 4 * back[1] / n))
                return

    def add_node(self, pid, nid, x, y, tier=4, type_id=24, size=3, enchant=0):
        self.nodes[nid] = (x, y)
        self.event(pid, "new_harvestable_object", {0: nid, 5: type_id, 7: tier, 8: [float(x), float(y)],
                                                   10: size, 11: enchant})

    def add_mob(self, pid, mid, x, y, hp=100, zone=None, name=""):
        self.mobs[mid] = [x, y, hp, zone or self.zone[pid], name]
        if self.here(pid, self.mobs[mid][3]):
            self._send_mob(pid, mid)

    def _send_mob(self, pid, mid):
        x, y, hp, _z, name = self.mobs[mid]
        self.event(pid, "new_mob", {0: mid, 1: 5, 7: [float(x), float(y)], 13: float(hp), 14: 100.0, 32: name})

    def add_loot(self, pid, lid, x, y, chest=False, zone=None):
        self.loot[lid] = (x, y, zone or self.zone[pid], chest)
        if self.here(pid, self.loot[lid][2]):
            self._send_loot(pid, lid)

    def _send_loot(self, pid, lid):
        x, y, _z, chest = self.loot[lid]
        self.event(pid, "new_loot_chest" if chest else "new_loot", {0: lid, 3: [float(x), float(y)]})

    def add_link(self, pid, lid, x, y, zone, target, tx, ty, event="new_exit", name=""):
        self.links[lid] = (x, y, zone, target, tx, ty, event, name)
        if self.here(pid, zone):
            self._send_link(pid, lid)

    def _send_link(self, pid, lid):
        x, y, _z, _t, _tx, _ty, event, name = self.links[lid]
        params = {0: lid, 1: [float(x), float(y)]}
        if name:
            params[3] = name
        self.event(pid, event, params)

    def add_player(self, pid, cid, x, y, zone=None, name="Gank", faction=0):
        self.players[cid] = (x, y, zone or self.zone[pid], name, faction)
        if self.here(pid, self.players[cid][2]):
            self._send_player(pid, cid)

    def _send_player(self, pid, cid):
        x, y, _z, name, faction = self.players[cid]
        self.event(pid, "new_character", {0: cid, 1: name, 12: [float(x), float(y)], 53: faction})

    def screen_to_world(self, pid, fx, fy):
        w = self.windows[pid]
        aspect = w.rect[2] / w.rect[3]
        sx, sy = (fx - self.center[0]) * aspect, fy - self.center[1]
        k = 1.0 - 0.25 * sy          # перспектива: верх экрана «дальше»
        x0, y0 = self.pos[pid]
        return (x0 + (TRUE_M[0][0] * sx + TRUE_M[0][1] * sy) * k,
                y0 + (TRUE_M[1][0] * sx + TRUE_M[1][1] * sy) * k)

    def click(self, pid, fx, fy, button):
        if self.ignore_clicks:
            return
        pts = self.manager.config["points"]
        if self.opened is not None and pts.get("loot_all") and \
                math.hypot(fx - pts["loot_all"][0], fy - pts["loot_all"][1]) < 0.01:
            for _ in range(self.items_per_loot):
                self.event(pid, "inventory_put_item", {0: pid})
            self.event(pid, "leave", {0: self.opened})
            self.loot.pop(self.opened, None)
            self.opened = None
            return
        if any(math.hypot(fx - x, fy - y) < 0.01 for x, y in pts.values()):
            self.request(pid, 99, {})         # кнопка интерфейса, а не клик по земле
            return
        tx, ty = self.screen_to_world(pid, fx, fy)
        x0, y0 = self.pos[pid]
        zone = self.zone[pid]
        # Порталы и выходы данжей — по клику (в игре вход в них — действие с задержкой).
        for lx, ly, lzone, target, ttx, tty, _ev, _name in list(self.links.values()):
            if lzone == zone and math.hypot(lx - tx, ly - ty) < 2.0 and math.hypot(lx - x0, ly - y0) < 8:
                self.join(pid, self.manager.feed_for(pid).character, target, (ttx, tty))
                return
        for mid, (mx, my, _hp, mzone, _n) in self.mobs.items():
            if mzone == zone and math.hypot(mx - tx, my - ty) < 2.0:
                self.attacking = mid
                return
        for lid, (lx, ly, lzone, _c) in self.loot.items():
            if lzone == zone and math.hypot(lx - tx, ly - ty) < 2.0 and math.hypot(lx - x0, ly - y0) < 6:
                self.move_to(pid, lx + 1, ly)
                self.opened = lid
                return
        for nid, (nx, ny) in list(self.nodes.items()):
            if math.hypot(nx - tx, ny - ty) < 1.5 and math.hypot(nx - x0, ny - y0) < 6:
                self.move_to(pid, nx + 1, ny)
                if self.harvest_ok:
                    for _ in range(self.items_per_harvest):
                        self.event(pid, "inventory_put_item", {0: pid})
                    self.event(pid, "harvestable_change_state", {0: nid, 1: 1})
                    self.event(pid, "harvest_finished", {0: pid})
                    self.event(pid, "harvestable_change_state", {0: nid, 1: 0})
                    del self.nodes[nid]
                return
        self.move_to(pid, tx, ty)

    def tick(self):
        """Время идёт: быстрый выход завершается (или его сбивает урон)."""
        if not self.exiting:
            return
        pid, until = self.exiting
        if self.damage_on_exit:
            self.damage_on_exit -= 1
            self.exiting = None
            self.my_hp = (self.my_hp or 1000.0) - 50
            self.event(pid, "health_update", {0: pid, 3: float(self.my_hp)})
            return
        if self.clock() >= until:
            self.exiting = None
            target, pos = self.exit_map[self.zone[pid]]
            self.join(pid, self.manager.feed_for(pid).character, target, pos)

    def key(self, pid, combo):
        self.request(pid, 99, {})
        if combo == "a" and self.zone[pid] in self.exit_map:
            self.exiting = (pid, self.clock() + 10)
            return
        mid = self.attacking
        if mid in self.mobs and combo in ("q", "w", "e"):
            mob = self.mobs[mid]
            mob[2] -= 40
            if mob[2] <= 0:
                del self.mobs[mid]
                self.attacking = None
                self.event(pid, "leave", {0: mid})
                self.next_id += 1
                self.add_loot(pid, self.next_id, mob[0] + 1, mob[1])
            else:
                self.event(pid, "health_update", {0: mid, 3: float(mob[2])})


def make(tmp, prices=None, maps=None, index=None):
    clock = Clock()
    game = FakeGame()
    desk = FakeDesktop(game)
    prices = prices or {}
    zm = FakeZonemaps(maps, index)
    alerts = []
    mgr = BotManager(Path(tmp) / "bots.json", make_radar=lambda: Radar(clock=clock), desktop=desk,
                     notify=lambda key, title, text: alerts.append((key, title, text)),
                     price_of=lambda item, loc, side: prices.get((item, side)),
                     item_name=lambda i: {"T4_BAG": "Сумка адепта"}.get(i, i),
                     zonemaps=zm, zone_name=zm.zone_name, clock=clock, sleep=clock.sleep)
    mgr.config["enabled"] = True
    mgr.alerts = alerts
    game.manager = mgr
    game.clock = clock
    game.index = zm.idx
    clock.on_sleep.append(game.tick)
    return mgr, game, desk, clock


class Base(unittest.TestCase):
    prices = None
    maps = None
    index = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr, self.game, self.desk, self.clock = make(self.tmp.name, self.prices, self.maps, self.index)
        self.bot = self.mgr.bot

    def tearDown(self):
        self.bot.stop()
        if self.bot.thread:
            self.bot.thread.join(5)
        self.tmp.cleanup()

    def window(self, name="Alice", pos=(0.0, 0.0), zone="0201", pid=1):
        self.game.add_window(pid, name, pos, zone)
        self.bot.pid = pid
        return pid

    def texts(self):
        return [x["text"] for x in self.bot.log]


# --- чистые функции ---------------------------------------------------------------
class GeometryTest(unittest.TestCase):
    def test_round_trip_and_clamp(self):
        cal = Calibration()
        for w in ((10, 0), (0, -7), (3.5, 12)):
            back = cal.to_world(*cal.to_screen(*w))
            self.assertAlmostEqual(back[0], w[0], places=6)
            self.assertAlmostEqual(back[1], w[1], places=6)
        sx, sy = cal.clamp_step(5, 0, 16 / 9)
        self.assertAlmostEqual(math.hypot(sx, sy), 0.26)
        sx, sy = cal.clamp_step(0, -0.6, 16 / 9, step=1.0)
        self.assertAlmostEqual(sy, 0.07 - 0.5)
        fx, fy = cal.fractions(0.16, 0.1, 16 / 9)
        self.assertAlmostEqual(fx, 0.5 + 0.09)
        self.assertAlmostEqual(fy, 0.6)

    def test_far_targets_keep_direction_with_perspective(self):
        cal = Calibration(m=[[14.2, 19.8], [15.5, -23.1]], p=-0.26)
        for w in ((-200, -150), (300, -400), (1000, 1000), (-10, 5), (5, -6), (0, 900)):
            s = cal.to_screen(*w)
            k = min(1.0, 0.3 / math.hypot(*s))
            back = cal.to_world(s[0] * k, s[1] * k)
            cos = (back[0] * w[0] + back[1] * w[1]) / (math.hypot(*back) * math.hypot(*w))
            self.assertGreater(cos, 0.99, w)
        near = cal.to_world(*cal.to_screen(4.0, -3.0))
        self.assertAlmostEqual(near[0], 4.0, places=4)
        self.assertAlmostEqual(near[1], -3.0, places=4)

    def test_broken_calibration(self):
        with self.assertRaises(BotError):
            Calibration(m=[[1, 2], [2, 4]]).to_screen(1, 1)
        self.assertEqual(Calibration.from_dict(None).m, Calibration().m)
        c = Calibration.from_dict({"cx": 0.4, "cy": 0.55, "m": [[1, 2], [3, 4]], "measured": True})
        self.assertEqual((c.cx, c.cy, c.m, c.measured), (0.4, 0.55, [[1.0, 2.0], [3.0, 4.0]], True))

    def test_solve(self):
        d = 0.15
        cal = solve_calibration(d, (2.1, 2.4), (-2.1, -2.4), (3.0, -3.45), (-3.0, 3.45))
        self.assertTrue(cal.measured)
        self.assertEqual((cal.cx, cal.cy), (0.5, 0.5))
        self.assertAlmostEqual(cal.m[0][0], 14.0)
        self.assertAlmostEqual(cal.m[1][1], -23.0)
        with self.assertRaisesRegex(BotError, "почти не двигался"):
            solve_calibration(d, (0.01, 0), (0, 0), (0, 0.01), (0, 0))
        with self.assertRaisesRegex(BotError, "в одну сторону"):
            solve_calibration(d, (2, 2), (-2, -2), (2.1, 2.0), (-2.1, -2.0))


class MacroTest(unittest.TestCase):
    def test_parse_all_steps_and_points(self):
        steps = parse_macro("# открыть рынок\nclick 0.5 0.25\nrclick 0.1 0.9\nclick @search\ntype {name}\n"
                            "key ctrl+a\nwait 500\nexpect 3000\n", {"search": [0.3, 0.2]})
        self.assertEqual(steps, [("click", 0.5, 0.25), ("rclick", 0.1, 0.9), ("click", 0.3, 0.2),
                                 ("type", "{name}"), ("key", "ctrl+a"), ("wait", 500), ("expect", 3000)])
        self.assertEqual(parse_macro("click @search")[0], ("click", 0.5, 0.5))   # проверка без точек

    def test_parse_errors_name_the_line(self):
        for text, msg in (("click 2 0.5", "строка 1: координаты"), ("\nclick x", "строка 2: неверные"),
                          ("key hyper", "строка 1: неизвестная клавиша"), ("jump 1", "неизвестное действие"),
                          ("wait -5", "время от 0"), ("click @nowhere", "нет такой точки")):
            with self.assertRaisesRegex(ValueError, msg):
                parse_macro(text)
        with self.assertRaisesRegex(ValueError, "Поле поиска предмета.*не указана"):
            parse_macro("click @search", {})

    def test_templates_use_only_known_points(self):
        for name, text in TEMPLATES.items():
            self.assertTrue(parse_macro(text, ALL_POINTS), name)

    def test_helpers(self):
        self.assertEqual(fill("{name} x{qty} {unknown}", {"name": "Сумка", "qty": 2}), "Сумка x2 {unknown}")
        self.assertEqual(market_price(1000, "sell", 1), 999)
        self.assertEqual(market_price(1000, "buy", 5), 1005)
        self.assertEqual(market_price(1, "sell", 5), 1)
        self.assertIsNone(market_price(None, "sell", 1))
        self.assertEqual([(s.key, s.cd) for s in parse_skills("q:3 W:10, e")], [("q", 3.0), ("w", 10.0), ("e", 5.0)])
        with self.assertRaisesRegex(ValueError, "не число"):
            parse_skills("q:x")
        with self.assertRaises(ValueError):
            parse_skills("zz:1")
        self.assertEqual(split_items("T4_BAG, T5_BAG\nT6_BAG"), ["T4_BAG", "T5_BAG", "T6_BAG"])


class FeedTest(unittest.TestCase):
    def test_feed_tracks_own_client(self):
        clock = Clock()
        f = ClientFeed(lambda: Radar(clock=clock), clock=clock)
        f.feed(pb.packet(pb.response(DEFAULT_OPCODES["join"], {0: 7, 2: "Bob", 8: "3004", 9: [1.0, 2.0]})))
        f.feed(pb.packet(pb.request(DEFAULT_OPCODES["move"], {1: [5.0, 6.0]})))
        f.feed(pb.packet(pb.event(DEFAULT_EVENTS["new_harvestable_object"], {0: 3, 5: 24, 7: 5, 8: [9.0, 9.0], 10: 4})))
        f.feed(pb.packet(pb.event(DEFAULT_EVENTS["regeneration_health_changed"], {0: 7, 2: 500.0, 3: 1000.0})))
        f.feed(pb.packet(pb.event(DEFAULT_EVENTS["health_update"], {0: 7, 3: 250.0})))
        f.feed(pb.packet(pb.event(DEFAULT_EVENTS["health_update"], {0: 99, 3: 1.0})))    # чужое
        self.assertEqual((f.character, f.location, f.zone), ("Bob", "3004", "3004"))
        self.assertEqual((f.me["x"], f.me["y"]), (5.0, 6.0))
        self.assertEqual(f.requests, 1)
        self.assertEqual(f.request_log[-1][1], "move")
        self.assertEqual([e.res for e in f.entities("resource")], ["ore"])
        self.assertIsNotNone(f.entity(3))
        self.assertEqual(f.hp_pct, 25.0)
        self.assertEqual(f.radar.pending_nodes, [])


# --- менеджер и окно --------------------------------------------------------------
class ManagerTest(Base):
    def test_traffic_split_by_window_and_target(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.add_window(2, "Bob", (100, 100), zone="3004")
        self.clock.t += 1
        self.game.move_to(2, 110, 100)
        self.assertEqual(self.mgr.feed_for(2).me["x"], 110)
        self.assertEqual(self.mgr.feed_for(1).me["x"], 0)
        self.desk.fg = 20                       # активно окно Bob
        self.assertEqual(self.mgr.target(), 2)
        self.desk.fg = 999                      # активно чужое окно — последнее с трафиком
        self.assertEqual(self.mgr.target(), 2)
        self.clock.t += 1
        self.game.move_to(1, 2, 0)
        self.assertEqual(self.mgr.target(), 1)
        self.bot.pid = 2                        # бот уже работал с окном Bob
        self.assertEqual(self.mgr.target(), 2)
        snap = self.mgr.snapshot()
        self.assertEqual((snap["game"]["windows"], snap["game"]["character"]), (2, "Bob"))

    def test_no_window_and_unknown_character(self):
        with self.assertRaisesRegex(BotError, "окно игры не найдено"):
            self.mgr.command({"action": "start", "task": "gather"})
        self.assertIsNone(self.mgr.snapshot()["game"])
        self.game.windows[3] = GameWindow(30, 3, "Albion Online", rect=(0, 0, 800, 600), ports=[50003])
        self.mgr.refresh()
        with self.assertRaisesRegex(BotError, "ещё не известен"):
            self.mgr.command({"action": "calibrate"})
        self.bot.pid = 3
        with self.assertRaisesRegex(BotError, "нет трафика"):
            _ = self.bot.feed

    def test_disabled_ignores_traffic(self):
        self.mgr.config["enabled"] = False
        self.mgr.on_packet(50001, pb.packet(pb.request(21, {1: [1.0, 1.0]})))
        self.assertEqual(self.mgr.feeds, {})

    def test_feed_limit(self):
        for port in range(70000 - 70, 70000):
            self.mgr.on_packet(port, pb.packet(pb.request(21, {1: [1.0, 1.0]})))
        self.assertLessEqual(len(self.mgr.feeds), 65)

    def test_only_one_bot_runs(self):
        self.window()
        self.mgr.command({"action": "start", "task": "wander"})
        first = self.bot.thread
        self.mgr.command({"action": "start", "task": "wander"})
        self.assertFalse(first.is_alive())          # прежняя задача остановлена
        self.assertTrue(self.bot.running)
        self.assertEqual(self.mgr.config["task"], "wander")
        self.mgr.command({"action": "stop"})
        self.bot.thread.join(5)
        self.assertEqual(self.bot.status, "остановлен")
        with self.assertRaisesRegex(BotError, "неизвестная задача"):
            self.bot.start("dance", 1)
        with self.assertRaisesRegex(BotError, "неизвестная задача"):
            self.mgr.command({"action": "start", "task": "dance"})

    def test_stop_key_and_window_close(self):
        self.window()
        started = threading.Event()
        self.bot.thread = threading.Thread(target=lambda: (started.set(), self.bot.stop_event.wait(5)))
        self.bot.thread.start()
        started.wait(1)
        self.desk.pressed.add(VK["f12"])
        self.mgr.tick()
        self.assertTrue(self.bot.stop_event.is_set())
        self.assertIn("F12", self.mgr.message)
        self.bot.thread.join(1)
        self.bot.stop_event.clear()
        del self.game.windows[1]
        self.mgr.refresh()
        self.assertTrue(self.bot.stop_event.is_set())

    def test_waits_while_user_is_active_and_restores_focus(self):
        self.window()
        self.desk.idle = 0.5
        statuses = []

        def user_leaves():
            statuses.append(self.bot.status)
            if self.clock.t > 1003:
                self.desk.idle = 1e9
        self.clock.on_sleep.append(user_leaves)
        self.desk.fg, self.desk.cur = 777, (5, 6)
        self.bot.click_at(0.5, 0.6)
        self.assertIn("пауза: вы за компьютером", statuses)
        self.assertEqual(len(self.desk.clicks), 1)
        self.assertEqual(self.desk.focused, [10, 777])
        self.assertEqual(self.desk.moved_cursor, [(5, 6)])

    def test_focus_refused_window_gone_and_background(self):
        self.window()
        self.desk.focus_ok = False
        with self.assertRaisesRegex(BotError, "переключиться"):
            self.bot.click_at(0.5, 0.5)
        self.desk.alive = False
        with self.assertRaisesRegex(BotError, "закрыто"):
            self.bot.click_at(0.5, 0.5)
        self.desk.alive = self.desk.focus_ok = True
        self.desk.focused.clear()
        self.mgr.config["input"] = "background"
        self.bot.click_at(0.5, 0.5)
        self.assertEqual(self.desk.focused, [])
        self.assertTrue(self.desk.clicks[-1][4])


class CalibrationTest(Base):
    def test_finds_camera_and_character(self):
        self.window()
        self.game.center = (0.47, 0.56)
        cal = self.bot.calibrate()
        for got, want in zip(sum(cal.m, []), sum(TRUE_M, [])):
            self.assertAlmostEqual(got, want, delta=abs(want) * 0.1)
        self.assertAlmostEqual(cal.cx, 0.47, delta=0.01)
        self.assertAlmostEqual(cal.cy, 0.56, delta=0.015)
        self.assertAlmostEqual(cal.p, -0.25, delta=0.05)          # перспектива модели
        # Один клик далеко от персонажа попадает почти точно (перспектива учтена).
        for target in ((6.0, 4.0), (-5.0, -3.0), (2.0, -7.0), (-6.0, 5.0)):
            x0, y0 = self.game.pos[1]
            self.bot.click_world(target[0], target[1], step=0.45)
            x1, y1 = self.game.pos[1]
            self.assertLess(math.hypot(x1 - x0 - target[0], y1 - y0 - target[1]), 0.6, target)
        saved = json.loads((Path(self.tmp.name) / "bots.json").read_text())["calib"]["1600x900"]
        self.assertTrue(saved["measured"])
        self.game.add_node(1, 801, 2.0, 1.0)
        self.assertTrue(self.bot.harvest(self.mgr.feed_for(1).entity(801)))   # клик по узлу точно в цель

    def test_refuses_next_to_resource_and_silent_game(self):
        self.window()
        self.game.add_node(1, 1001, 3, 2)
        with self.assertRaisesRegex(BotError, "рядом ресурс"):
            self.bot.calibrate()
        self.game.nodes.clear()
        self.game.event(1, "leave", {0: 1001})
        self.game.ignore_clicks = True
        with self.assertRaisesRegex(BotError, "клики не доходят"):
            self.bot.calibrate()

    def test_command_runs_calibration_and_test_click(self):
        self.window()
        self.mgr.command({"action": "calibrate"})
        self.bot.thread.join(5)
        self.assertTrue(self.mgr.snapshot()["game"]["calibrated"])
        self.mgr._test_click(1, 0.5, 0.7)
        self.assertIn("ответила запросом", self.texts()[-1])
        self.game.ignore_clicks = True
        self.mgr._test_click(1, 0.5, 0.7)
        self.assertIn("не отправила запрос", self.texts()[-1])


# --- ходьба и маршруты ------------------------------------------------------------
class WalkTest(Base):
    maps = {"WALLED": {"bounds": [-60, -60, 60, 60],
                       "tiles": [["building", 0.0, 0.0, 6.0, 60.0, 0, 0.0]]}}

    def test_walk_reaches_far_point_and_gives_up_when_stuck(self):
        self.window()
        self.bot.calibrate()
        self.assertTrue(self.bot.walk_to(60, -40, tol=2))
        x, y = self.game.pos[1]
        self.assertLess(math.hypot(x - 60, y + 40), 2.0)
        self.mgr.config["calib"] = {}                 # без калибровки (умолчание) — короткими шагами
        self.assertTrue(self.bot.walk_to(0, 0, tol=2.5))
        self.game.ignore_clicks = True
        self.assertFalse(self.bot.walk_to(50, 0))

    def test_path_goes_around_wall_from_zone_map(self):
        self.window(zone="WALLED", pos=(-20.0, 0.0))
        self.game.walls = [(-3, -30, 3, 30)]
        self.bot.calibrate()
        self.game.join(1, "Alice", "WALLED", (-20.0, 0.0))
        self.assertTrue(self.bot.walk_path(20, 0))
        self.assertLess(math.hypot(self.game.pos[1][0] - 20, self.game.pos[1][1]), 3)

    def test_walking_click_avoids_other_nodes(self):
        self.window()
        self.bot.calibrate()
        _fx, _fy, wx, wy = self.bot.plan_click(40, 0, 0.26)
        self.game.add_node(1, 1002, wx, wy)
        self.game.add_node(1, 1003, 40, 0)
        self.bot.click_world(40, 0, keep_clear_of=1003)
        self.assertIn(1002, self.game.nodes)
        x, y = self.game.pos[1]
        self.assertGreater(math.hypot(x - wx, y - wy), 2.0)

    def test_travel_between_zones(self):
        self.window(zone="CITYA", pos=(0.0, 0.0))
        self.bot.calibrate()
        self.bot.travel_to("CITYB", 10, 5, safety="yellow")
        self.assertEqual(self.game.zone[1], "CITYB")
        self.assertLess(math.hypot(self.game.pos[1][0] - 10, self.game.pos[1][1] - 5), 3)
        self.assertIn("перешёл в «Дорога»", self.texts())
        self.assertIn("перешёл в «Город Б»", self.texts())

    def test_travel_respects_safety_and_errors(self):
        self.window(zone="CITYA", pos=(0.0, 0.0))
        self.bot.calibrate()
        with self.assertRaisesRegex(BotError, "нет пути"):
            self.bot.travel_to("CITYB", 0, 0, safety="safe")      # дорога жёлтая
        with self.assertRaisesRegex(BotError, "нет сохранённого места"):
            self.bot.go_place("банк")
        self.mgr.zonemaps = None
        self.mgr._router = None
        with self.assertRaisesRegex(BotError, "нет списка зон"):
            self.bot.travel_to("CITYB", 0, 0)


# --- задачи -----------------------------------------------------------------------
class GatherTest(Base):
    def test_gathering_collects_matching_nodes(self):
        self.window()
        self.mgr.command({"action": "configure", "gather": {"res": ["ore"], "tier_min": 4, "radius": 60}})
        self.mgr.config["rest_min"] = 0
        self.game.add_node(1, 501, 20, 10, tier=4)
        self.game.add_node(1, 502, -15, 5, tier=5)
        self.game.add_node(1, 503, 5, 12, tier=4, type_id=1)    # дерево — не наш вид
        self.game.add_node(1, 504, 12, -6, tier=3)              # T3 — ниже порога
        self.game.add_node(1, 505, 200, 0, tier=6)              # слишком далеко
        self.clock.limit = self.clock.t + 600
        self.bot.run("gather")
        self.assertEqual(self.bot.status, "остановлен")
        self.assertEqual(self.bot.gathered, 2)
        self.assertEqual(set(self.game.nodes), {503, 504, 505})
        self.assertTrue(any("калибровка готова" in t for t in self.texts()))
        self.assertTrue(any("собран ore T4" in t for t in self.texts()))

    def test_three_failed_nodes_stop_and_zone_change_moves_home(self):
        self.window()
        self.game.harvest_ok = False
        for i in range(4):
            self.game.add_node(1, 900 + i, 10 + 4 * i, 3)
        self.clock.limit = self.clock.t + 5000
        self.bot.run("gather")
        self.assertEqual(self.bot.status, "ошибка")
        self.assertIn("три узла подряд", self.texts()[-1])
        self.bot.fails_in_row = 0
        self.bot.home, self.bot.home_zone = (0, 0), "0201"
        self.game.join(1, "Alice", "0202", (500.0, 500.0))
        self.clock.limit = self.clock.t + 30
        with self.assertRaises(BotStopped):
            self.bot.gather_session(self.clock.t + 100)
        self.assertEqual((self.bot.home, self.bot.home_zone), ((500.0, 500.0), "0202"))

    def test_hostile_player_and_mobs_are_avoided(self):
        self.window()
        g = {**DEFAULTS["gather"], "avoid_mobs": 10}
        self.game.add_node(1, 701, 10, 0)
        self.game.add_mob(1, 900, 12.0, 0.0)
        self.assertIsNone(self.bot.pick_node(g))
        self.assertIsNotNone(self.bot.pick_node({**g, "avoid_mobs": 0}))
        self.assertEqual(self.bot.danger(g), "")
        self.game.event(1, "new_character", {0: 901, 1: "Gank", 12: [20.0, 0.0], 53: 255})
        self.assertEqual(self.bot.danger(g), "Gank")
        self.assertEqual(self.bot.danger({**g, "avoid_players": False}), "")


class MarketTest(Base):
    prices = {("T4_BAG", "sell"): 2500, ("T4_BAG", "buy"): 1800}

    def setUp(self):
        super().setUp()
        self.window("Trader", zone="CITYA")
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.mgr.config["rest_min"] = 0

    def test_sell_orders_with_built_in_template(self):
        self.mgr.command({"action": "configure", "market": {"items": "T4_BAG", "undercut": 10, "qty": 3,
                                                            "interval_min": 1, "interval_max": 1, "orders": 2}})
        self.bot.run("market")
        self.assertEqual(self.bot.status, "готово", self.texts()[-2:])
        self.assertEqual(self.bot.orders, 2)
        typed = [t for _p, t in self.desk.typed]
        self.assertEqual(typed[:3], ["Сумка адепта", "2490", "3"])
        clicked = [fx for _p, fx, *_ in self.desk.clicks]
        self.assertTrue(any(abs(fx - ALL_POINTS["sell_tab"][0]) < 0.01 for fx in clicked))
        self.assertIn((1, "esc"), self.desk.keys)
        self.assertTrue(any("продажа: Сумка адепта × 3 по 2490" in t for t in self.texts()))

    def test_buy_side_missing_price_and_custom_macro(self):
        self.mgr.command({"action": "save_macro", "name": "мой", "text": "click 0.2 0.1\ntype {price}\nexpect 2000"})
        self.mgr.command({"action": "configure", "market": {"items": "T4_BAG T5_BAG", "side": "buy", "undercut": 1,
                                                            "macro": "мой", "orders": 3}})
        self.bot.rng.seed(3)
        self.clock.limit = self.clock.t + 6000
        self.bot.run("market")
        self.assertIn((1, "1801"), self.desk.typed)
        self.assertTrue(any("T5_BAG: нет цены" in t for t in self.texts()))

    def test_market_walks_to_saved_place(self):
        self.mgr.config["places"]["рынок"] = {"zone": "CITYA", "x": 20.0, "y": 5.0}
        self.mgr.command({"action": "configure", "market": {"items": "T4_BAG", "place": "рынок", "orders": 1}})
        self.bot.run("market")
        self.assertEqual(self.bot.status, "готово", self.texts()[-2:])
        self.assertLess(math.hypot(self.game.pos[1][0] - 20, self.game.pos[1][1] - 5), 3)

    def test_errors(self):
        for market, msg in (({"items": ""}, "не задан список"), ({"items": "T4_BAG", "macro": "nope"}, "нет макроса")):
            self.mgr.command({"action": "configure", "market": market})
            self.bot.run("market")
            self.assertEqual(self.bot.status, "ошибка")
            self.assertIn(msg, self.texts()[-1])
        self.mgr.config["points"].pop("sell_tab")
        self.mgr.command({"action": "configure", "market": {"items": "T4_BAG", "macro": ""}})
        self.bot.run("market")
        self.assertIn("Вкладка «Продать» в окне рынка» не указана", self.texts()[-1])
        self.mgr.config["macros"]["silent"] = "click 0.5 0.5\nwait 100\nexpect 1000"
        self.game.ignore_clicks = True
        with self.assertRaisesRegex(BotError, "не отправила запрос"):
            self.bot.run_macro("silent", {})


class TransportTest(Base):
    prices = {("T4_ORE", "sell"): 100}

    def setUp(self):
        super().setUp()
        self.window("Carrier", zone="CITYA", pos=(0.0, 0.0))
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.mgr.config["places"] = {"банк А": {"zone": "CITYA", "x": 0.0, "y": 10.0},
                                     "рынок Б": {"zone": "CITYB", "x": 10.0, "y": 0.0}}
        self.mgr.config["macros"]["погрузка"] = "click 0.31 0.42\nexpect 2000"

    def test_round_trips_with_market_sale(self):
        self.mgr.command({"action": "configure", "transport": {
            "load_place": "банк А", "unload_place": "рынок Б", "load_macro": "погрузка", "unload": "market_sell",
            "sell_items": "T4_ORE", "safety": "yellow", "mount_key": "a", "trips": 2}})
        self.bot.run("transport")
        self.assertEqual(self.bot.status, "готово", self.texts()[-3:])
        self.assertEqual(self.bot.trips, 2)
        self.assertEqual(self.bot.orders, 2)
        self.assertEqual(self.game.zone[1], "CITYB")
        self.assertIn((1, "a"), self.desk.keys)
        self.assertIn("рейс 2 завершён", self.texts())

    def test_unload_macro_one_way_and_errors(self):
        self.mgr.config["macros"]["разгрузка"] = "click 0.6 0.6\nexpect 2000"
        self.mgr.command({"action": "configure", "transport": {
            "load_place": "банк А", "unload_place": "рынок Б", "unload": "macro", "unload_macro": "разгрузка",
            "safety": "red", "round_trip": False}})
        self.bot.run("transport")
        self.assertEqual((self.bot.status, self.bot.trips), ("готово", 1), self.texts()[-3:])
        self.mgr.command({"action": "configure", "transport": {"load_place": ""}})
        self.bot.run("transport")
        self.assertIn("выберите место погрузки", self.texts()[-1])
        self.mgr.command({"action": "configure", "transport": {"load_place": "банк А", "unload": "market_sell",
                                                               "sell_items": ""}})
        self.bot.run("transport")
        self.assertIn("задайте список предметов", self.texts()[-1])
        self.mgr.command({"action": "configure", "transport": {"sell_items": "T4_ORE", "safety": "safe"}})
        self.bot.run("transport")
        self.assertIn("нет пути", self.texts()[-1])


class DungeonTest(Base):
    def setUp(self):
        super().setUp()
        self.window("Hero", zone="DUNGEON1", pos=(0.0, 0.0))
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.bot.calibrate()

    def test_clears_mobs_opens_chest_and_loots(self):
        self.game.add_mob(1, 301, 12, 0)
        self.game.add_mob(1, 302, 18, 8)
        self.game.add_loot(1, 401, -14, -6, chest=True)
        self.mgr.command({"action": "configure", "dungeon": {"skills": "q:1 w:2", "explore_min": 1, "chest_wait": 2}})
        self.bot.run("dungeon")
        self.assertEqual(self.bot.status, "готово", self.texts()[-3:])
        self.assertEqual(self.bot.kills, 2)
        self.assertEqual(self.bot.loots, 3)           # две сумки с мобов и сундук
        self.assertEqual((self.game.mobs, self.game.loot), ({}, {}))
        self.assertIn("данж пройден", self.texts()[-1])

    def test_potion_retreat_and_settings(self):
        self.mgr.command({"action": "configure", "dungeon": {"potion_key": "2", "potion_hp": 50, "retreat_hp": 30}})
        self.bot.home = (0.0, 0.0)
        self.game.move_to(1, 15, 0)
        self.game.event(1, "regeneration_health_changed", {0: 1, 2: 200.0, 3: 1000.0})
        healed = []

        def regen():
            if not healed and self.clock.t > 1100:
                healed.append(1)
                self.game.event(1, "health_update", {0: 1, 3: 950.0})
        self.clock.on_sleep.append(regen)
        self.bot.potion_at = -1e9
        self.bot.heal(self.mgr.task_config("dungeon"))
        self.assertIn((1, "2"), self.desk.keys)
        self.assertTrue(any("отход к входу" in t for t in self.texts()))
        self.assertLess(math.hypot(*self.game.pos[1]), 4)
        self.mgr.command({"action": "configure", "dungeon": {"skills": "zz:1"}})
        self.bot.run("dungeon")
        self.assertIn("умения", self.texts()[-1])

    def test_fight_timeout_and_time_limit(self):
        self.game.add_mob(1, 303, 10, 0, hp=10_000)
        self.mgr.command({"action": "configure", "dungeon": {"skills": "q:1", "max_min": 2}})
        self.bot.run("dungeon")
        self.assertTrue(any("бой затянулся" in t for t in self.texts()))
        self.assertIn("время на данж вышло", self.texts()[-1])


class DungeonRunTest(Base):
    """Цикл: город А → дорога (портал) → этаж 1 → этаж 2 (босс, сундук) → назад → сундук в городе."""

    def setUp(self):
        super().setUp()
        self.window("Hero", zone="CITYA", pos=(0.0, 0.0))
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.mgr.config["places"] = {"сундук": {"zone": "CITYA", "x": 0.0, "y": 10.0}}
        self.bot.calibrate()
        self.game.join(1, "Hero", "CITYA", (0.0, 0.0))
        g = self.game
        g.add_link(1, 700, 20, 30, "ROAD", "DNG1", 0.0, 0.0, "new_random_dungeon_exit", "RANDOMDUNGEON_SOLO_FOREST")
        g.add_link(1, 701, -20, -25, "ROAD", "DNGX", 0.0, 0.0, "new_random_dungeon_exit", "RANDOMDUNGEON_GROUP_X")
        g.add_link(1, 710, 0, -6, "DNG1", "ROAD", 24.0, 30.0)                 # этаж 1: выход назад
        g.add_link(1, 711, 30, 10, "DNG1", "DNG2", 0.0, 0.0)                  # этаж 1 → этаж 2
        g.add_link(1, 720, 0, -6, "DNG2", "DNG1", 27.0, 10.0)                 # этаж 2: выход назад
        g.add_mob(1, 801, 12, 4, zone="DNG1")
        g.add_mob(1, 802, 14, 0, zone="DNG2", name="T4_MOB_BOSS_FOREST")
        g.add_loot(1, 803, -12, 8, chest=True, zone="DNG2")
        self.mgr.command({"action": "configure", "dungeon": {
            "home_place": "сундук", "skills": "q:1 w:2", "explore_min": 0.5, "chest_wait": 2, "runs": 1,
            "safety": "yellow", "search_zones": 2, "search_min": 2}})

    def test_full_run_from_city_and_back(self):
        self.bot.run("dungeon_run")
        self.assertEqual(self.bot.status, "готово", self.texts()[-4:])
        self.assertEqual(self.bot.runs, 1)
        self.assertEqual((self.bot.kills, self.bot.loots), (2, 3))      # сумки с мобов и босса, сундук
        self.assertTrue(self.bot.boss_killed)
        self.assertEqual([lid for lid, lt in self.game.loot.items() if lt[2] == "DNG2"], [])
        self.assertEqual(self.game.zone[1], "CITYA")
        self.assertLess(math.hypot(self.game.pos[1][0], self.game.pos[1][1] - 10), 3)
        texts = self.texts()
        for t in ("ищу данж в «Дорога»", "нашёл портал: RANDOMDUNGEON_SOLO_FOREST", "вошёл в данж", "этаж 2",
                  "босс побеждён: T4_MOB_BOSS_FOREST", "финальный сундук открыт", "вышел из данжа в «Дорога»",
                  "добыча сдана в сундук"):
            self.assertTrue(any(t in x for x in texts), t)
        stash = [ALL_POINTS["stash_open"][0], ALL_POINTS["stash_deposit"][0]]
        clicked = [fx for _p, fx, *_ in self.desk.clicks]
        self.assertTrue(all(any(abs(fx - x) < 0.01 for fx in clicked) for x in stash))

    def test_skips_portal_with_players_and_wrong_kind(self):
        self.game.add_player(1, 950, 22, 33, zone="ROAD")
        self.game.join(1, "Hero", "ROAD", (-46.0, 0.0))
        d = self.mgr.task_config("dungeon")
        self.assertIsNone(self.bot.pick_portal(d))                    # у зелёного игрок, групповой не нужен
        self.assertEqual(self.bot.pick_portal({**d, "portal_kinds": ["group"]}).id, 701)
        self.assertEqual(self.bot.pick_portal({**d, "avoid_players": False}).id, 700)

    def test_walks_away_from_players_in_open_world(self):
        self.game.join(1, "Hero", "ROAD", (0.0, 0.0))
        self.game.add_player(1, 951, 10, 0, zone="ROAD", name="Stalker")
        d = self.mgr.task_config("dungeon")
        self.assertTrue(self.bot.avoid_players(d))
        self.assertLess(self.game.pos[1][0], -10)                     # ушёл в противоположную сторону
        self.assertIn("уходит от игроков: Stalker", self.texts()[-1])
        self.assertFalse(self.bot.avoid_players({**d, "avoid_players": False}))

    def test_players_in_dungeon_make_bot_leave(self):
        self.game.add_player(1, 952, 8, 8, zone="DNG1")
        self.game.join(1, "Hero", "DNG1", (0.0, 0.0))
        self.bot.run("dungeon")
        self.assertEqual(self.bot.status, "готово", self.texts()[-3:])
        self.assertIn("рядом игроки — выхожу из данжа", self.texts())
        self.assertEqual(self.game.zone[1], "ROAD")

    def test_no_portals_errors_and_death(self):
        for lid in (700, 701):
            del self.game.links[lid]
        self.mgr.command({"action": "configure", "dungeon": {"search_min": 0.2}})
        self.bot.run("dungeon_run")
        self.assertEqual(self.bot.status, "ошибка")
        self.assertIn("подходящих данжей в ближайших зонах нет", self.texts()[-1])
        self.mgr.command({"action": "configure", "dungeon": {"home_place": ""}})
        self.bot.run("dungeon_run")
        self.assertIn("выберите место сундука", self.texts()[-1])
        self.mgr.command({"action": "configure", "dungeon": {"home_place": "нет такого"}})
        self.bot.run("dungeon_run")
        self.assertIn("нет сохранённого места", self.texts()[-1])
        self.mgr.command({"action": "configure", "dungeon": {"home_place": "сундук", "safety": "safe"}})
        self.bot.run("dungeon_run")
        self.assertIn("нет зон с допустимой опасностью", self.texts()[-1])
        self.game.event(1, "regeneration_health_changed", {0: 1, 2: 0.0, 3: 1000.0})
        with self.assertRaisesRegex(BotError, "погиб"):
            self.bot.check_alive()

    def test_portal_kinds(self):
        from albion_trader.bot_dungeon import portal_kind
        self.assertEqual([portal_kind(n) for n in ("RANDOMDUNGEON_SOLO_X", "CORRUPTED_SOLO", "HELLGATE_2V2",
                                                   "ROADS_AVALON", "RANDOMDUNGEON_GROUP", "", "вход в данж")],
                         ["solo", "corrupted", "hellgate", "avalon", "group", "unknown", "unknown"])


class QuickExitTest(Base):
    def setUp(self):
        super().setUp()
        self.window("Hero", zone="CITYA", pos=(0.0, 0.0))
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.mgr.config["places"] = {"сундук": {"zone": "CITYA", "x": 0.0, "y": 10.0}}
        self.bot.calibrate()
        g = self.game
        g.add_link(1, 700, 20, 30, "ROAD", "DNG1", 0.0, 0.0, "new_random_dungeon_exit", "RANDOMDUNGEON_SOLO_X")
        g.add_link(1, 711, 30, 10, "DNG1", "DNG2", 0.0, 0.0)
        g.add_mob(1, 802, 14, 0, zone="DNG2", name="T4_MOB_BOSS")
        g.exit_map = {"DNG1": ("ROAD", (24.0, 30.0)), "DNG2": ("ROAD", (24.0, 30.0))}
        self.mgr.command({"action": "configure", "dungeon": {
            "home_place": "сундук", "skills": "q:1 w:2", "explore_min": 0.5, "runs": 1, "exit_key": "a",
            "exit_channel": 10, "safety": "yellow", "search_zones": 2, "search_min": 2}})

    def test_run_uses_quick_exit(self):
        self.bot.run("dungeon_run")
        self.assertEqual(self.bot.status, "готово", self.texts()[-4:])
        self.assertIn("быстрый выход: «Дорога»", self.texts())
        self.assertFalse(any("вышел из данжа" in t for t in self.texts()))     # пешком не шёл
        self.assertIn((1, "a"), self.desk.keys)
        self.assertEqual(self.game.zone[1], "CITYA")
        self.assertTrue(any(t == "Бот: данж пройден" for _k, t, _x in self.mgr.alerts))

    def test_damage_interrupts_then_retry_after_fighting(self):
        self.game.join(1, "Hero", "DNG1", (0.0, 0.0))
        self.game.add_mob(1, 803, 5, 5, zone="DNG1")
        self.game.damage_on_exit = 1
        self.bot.start_dungeon_state()
        d = self.mgr.task_config("dungeon")
        self.assertTrue(self.bot.quick_exit(d, self.bot.skills_of(d)))
        self.assertIn("быстрый выход сбит уроном — ещё раз", self.texts())
        self.assertNotIn(803, self.game.mobs)                    # моб рядом добит перед выходом
        self.assertEqual(self.game.zone[1], "ROAD")

    def test_falls_back_to_walking_out(self):
        self.game.add_link(1, 710, 0, -6, "DNG1", "ROAD", 24.0, 30.0)
        self.game.exit_map = {}                                  # клавиша не работает
        self.game.join(1, "Hero", "DNG1", (0.0, 0.0))
        self.bot.start_dungeon_state()
        self.bot.floors = [("DNG1", (0.0, 0.0))]
        self.bot.exit_dungeon(self.mgr.task_config("dungeon"))
        self.assertEqual(self.texts().count("быстрый выход не сработал"), 3)
        self.assertEqual(self.game.zone[1], "ROAD")
        self.assertTrue(any("вышел из данжа" in t for t in self.texts()))
        self.assertFalse(self.bot.quick_exit({"exit_key": ""}))


class WatchdogTest(Base):
    def test_stuck_bot_tries_to_recover_then_stops_with_alert(self):
        self.window()
        self.bot.calibrate()
        self.mgr.config["watchdog_min"] = 1
        self.game.ignore_clicks = True
        self.bot.run("wander")
        self.assertEqual(self.bot.status, "ошибка")
        self.assertIn("нет прогресса 1 мин", self.texts()[-1])
        self.assertGreaterEqual(self.texts().count("нет прогресса 1 мин — пробую выбраться"), 2)
        self.assertTrue(any(t == "Бот остановлен" for _k, t, _x in self.mgr.alerts))

    def test_idle_waits_are_not_stuck(self):
        self.window()
        self.mgr.config["watchdog_min"] = 1
        self.bot.progress_at = self.clock.t
        self.bot._progress_sig = None
        self.bot.watch_progress()
        self.bot.wait_idle(300)                 # 5 минут отдыха — не зависание
        self.assertEqual(self.bot.stuck_tries, 0)
        self.mgr.config["watchdog_min"] = 0     # выключен
        self.clock.t += 10_000
        self.bot.watch_progress()
        self.assertEqual(self.bot.stuck_tries, 0)


class SessionAndOverlayTest(Base):
    def test_session_is_recorded_and_summarized(self):
        from albion_trader.bot_session import read_log, read_traffic, summarize
        self.window(zone="CITYA")
        self.mgr.config["record"] = True
        self.game.add_link(1, 700, 20, 30, "CITYA", "ROAD", 0.0, 0.0, "new_random_dungeon_exit", "RANDOMDUNGEON_SOLO")
        self.bot.run("calibrate")
        self.assertIsNone(self.mgr.session)
        folders = list((Path(self.tmp.name) / "bot_sessions").iterdir())
        self.assertEqual(len(folders), 1)
        self.assertTrue(folders[0].name.endswith("-calibrate"))
        self.game.move_to(1, 1, 1)               # после сессии трафик не пишется
        packets = list(read_traffic(folders[0] / "traffic.bin"))
        self.assertTrue(packets)
        self.assertTrue(all(port == 50001 for _t, port, _p in packets))
        actions = read_log(folders[0] / "log.jsonl")
        self.assertEqual(actions[0]["a"], "start")
        self.assertEqual(sum(1 for a in actions if a["a"] == "click"), 6)
        self.assertTrue(any(a["a"] == "note" and "калибровка готова" in a["text"] for a in actions))
        # Сводка видит пакеты, клики и журнал (зона уже известна до записи — смен зон нет).
        text = summarize(folders[0], lambda: ClientFeed(lambda: Radar(clock=self.clock)))
        for s in ("Сессия бота:", "move", "click 6", "калибровка готова"):
            self.assertIn(s, text)

    def test_summary_shows_zones_and_unknown_codes(self):
        from albion_trader.bot_session import Session, summarize
        s = Session(Path(self.tmp.name), "test", clock=self.clock)
        s.packet(50001, pb.packet(pb.response(DEFAULT_OPCODES["join"], {0: 1, 2: "A", 8: "3004", 9: [1.0, 1.0]})))
        s.packet(50001, pb.packet(pb.event(4242, {0: 9, 1: [5.0, 5.0], 3: "MYSTERY"})))
        s.packet(50001, b"\x00\x01")           # мусор не мешает
        s.close()
        text = summarize(s.dir, lambda: ClientFeed(lambda: Radar(clock=self.clock)))
        self.assertIn("3004", text)
        self.assertIn("4242:", text)
        self.assertIn("(пусто)", text)
        again = Session(Path(self.tmp.name), "test", clock=self.clock)       # то же время — другая папка
        self.assertNotEqual(again.dir, s.dir)
        again.close()

    def test_cli_bot_session(self):
        from albion_trader.__main__ import main
        from unittest import mock
        with mock.patch("builtins.print") as out:
            self.assertEqual(main(["--data-dir", self.tmp.name, "bot-session"]), 1)
        self.assertIn("Записей сессий нет", out.call_args[0][0])
        self.window()
        self.mgr.config["record"] = True
        self.bot.run("calibrate")
        with mock.patch("builtins.print") as out:
            self.assertEqual(main(["--data-dir", self.tmp.name, "bot-session"]), 0)
        self.assertIn("Сессия бота", out.call_args[0][0])
        with mock.patch("builtins.print") as out:
            self.assertEqual(main(["--data-dir", self.tmp.name, "bot-session", self.tmp.name]), 1)

    def test_plan_and_overlay(self):
        self.window()
        self.bot.calibrate()
        self.assertIsNone(self.mgr.overlay())           # бот не работает — на радаре ничего
        self.bot.walk_to(30, 0)
        self.assertEqual(self.bot.plan["target"], [30, 0])
        self.assertEqual(self.bot.plan["zone"], "0201")
        visited = set()
        self.bot.mark_visited(visited)
        self.assertEqual(len(self.bot.plan["explored"]), 1)
        started = threading.Event()
        self.bot.thread = threading.Thread(target=lambda: (started.set(), self.bot.stop_event.wait(5)))
        self.bot.thread.start()
        started.wait(1)
        ov = self.mgr.overlay()
        self.assertEqual((ov["zone"], ov["target"]), ("0201", [30, 0]))
        self.bot.stop()


class SkillsAndThreatsTest(unittest.TestCase):
    def test_skill_conditions(self):
        from albion_trader.bot_core import skill_ready
        s = parse_skills("q:3 w:10@open e:20@boss 2:30@hp<40@self f:15@cast=1.5 r:9@hp>80")
        self.assertEqual([x.key for x in s], ["q", "w", "e", "2", "f", "r"])
        q, w, e, pot, f, r = s
        self.assertTrue(skill_ready(q, None, False, False))
        self.assertTrue(skill_ready(w, 90, False, True))
        self.assertFalse(skill_ready(w, 90, False, False))
        self.assertFalse(skill_ready(e, 90, False, True))
        self.assertTrue(skill_ready(e, 90, True, False))
        self.assertTrue(skill_ready(pot, 30, False, False))
        self.assertFalse(skill_ready(pot, 50, False, False))
        self.assertFalse(skill_ready(pot, None, False, False))
        self.assertTrue(pot.self_cast)
        self.assertEqual(f.cast, 1.5)
        self.assertFalse(skill_ready(r, 70, False, False))
        self.assertTrue(skill_ready(r, None, False, False))
        with self.assertRaisesRegex(ValueError, "непонятное условие"):
            parse_skills("q:3@sometimes")

    def test_threat_levels(self):
        from albion_trader.bot_threat import CAUTION, DANGER, ThreatTracker, friends_of
        clock = Clock()
        f = ClientFeed(lambda: Radar(clock=clock), clock=clock)
        f.feed(pb.packet(pb.response(DEFAULT_OPCODES["join"], {0: 1, 2: "Me", 8: "0201", 9: [0.0, 0.0]})))

        def player(cid, name, x, y, faction=0):
            f.feed(pb.packet(pb.event(DEFAULT_EVENTS["new_character"], {0: cid, 1: name, 12: [float(x), float(y)],
                                                                         53: faction})))
        d = {"avoid_players": True, "player_radius": 45, "friends": "Buddy; pal"}
        self.assertEqual(friends_of(d), {"buddy", "pal"})
        player(10, "Walker", 30, 0)
        player(11, "Buddy", 5, 0, 255)            # друг — не угроза даже с флагом
        player(12, "Far", 200, 0, 255)            # далеко
        tr = ThreatTracker()
        t = tr.assess(f, (0, 0), d)
        self.assertEqual([(x.name, x.level) for x in t], [("Walker", CAUTION)])
        f.feed(pb.packet(pb.event(DEFAULT_EVENTS["move"], {0: 10, 1: [20.0, 0.0]})))
        t = tr.assess(f, (0, 0), d)
        self.assertIn("приближается", t[0].reasons)
        player(13, "Gank", 25, 5, 255)
        t = tr.assess(f, (0, 0), d)
        self.assertEqual(t[0].name, "Gank")
        self.assertEqual(t[0].level, DANGER)
        self.assertIn("враждебный", t[0].text())
        self.assertIn("группа 2", t[0].reasons)


class CombatTest(Base):
    def setUp(self):
        super().setUp()
        self.window("Hero", zone="DNG1", pos=(0.0, 0.0))
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.bot.calibrate()
        self.game.join(1, "Hero", "DNG1", (0.0, 0.0))
        self.bot.start_dungeon_state()

    def d(self, **kw):
        return {**self.mgr.task_config("dungeon"), **kw}

    def test_prefers_small_packs_and_filters(self):
        g = self.game
        for i, (x, y) in enumerate(((10, 0), (11, 1), (10, -1), (12, 0))):   # группа из 4
            g.add_mob(1, 100 + i, x, y)
        g.add_mob(1, 200, -16, 0)                                               # одиночка дальше
        self.assertEqual(self.bot.pick_mob(self.d(max_pack=3), set()).id, 200)
        self.assertIn(self.bot.pick_mob(self.d(max_pack=0), set()).id, (100, 101, 102, 103))
        self.bot.mob_info = lambda m: {"tier": 8 if m.id == 200 else 4, "category": "champion" if m.id == 100 else ""}
        self.assertNotEqual(self.bot.pick_mob(self.d(max_pack=3, max_mob_tier=6), set()).id, 200)
        picked = self.bot.pick_mob(self.d(max_pack=0, skip_elite=True), set()).id
        self.assertNotEqual(picked, 100)
        self.assertEqual(self.bot.pick_mob(self.d(), set(), attacked=True).id, 100)

    def test_skill_profile_boss_only_and_cast(self):
        self.game.add_mob(1, 300, 6, 0)
        d = self.d()
        skills = parse_skills("e:1@boss q:1@cast=2")
        self.bot.fight(self.bot.feed.entity(300), d, skills, set())
        self.assertNotIn((1, "e"), self.desk.keys)            # «только по боссу» не нажата
        self.assertIn((1, "q"), self.desk.keys)
        self.game.add_mob(1, 301, 6, 2, name="T5_MOB_BOSS")
        self.bot.fight(self.bot.feed.entity(301), d, skills, set())
        self.assertIn((1, "e"), self.desk.keys)

    def test_kiting_steps_back(self):
        self.game.add_mob(1, 400, 2.5, 0, hp=10_000)
        start = self.clock.t
        self.clock.limit = start + 6
        with self.assertRaises(BotStopped):
            self.bot.fight(self.bot.feed.entity(400), self.d(kite=True, kite_dist=5), parse_skills("q:1"), set())
        self.assertLess(self.game.pos[1][0], -1.0)             # отошёл от моба (моб справа)

    def test_answers_hits_and_rests_between_packs(self):
        g = self.game
        g.add_mob(1, 500, 8, 0)
        self.bot.mob_info = lambda m: {"tier": 9}
        d = self.d(max_mob_tier=6, explore_min=0.2, rest_hp=60)
        g.event(1, "regeneration_health_changed", {0: 1, 2: 1000.0, 3: 1000.0})
        g.event(1, "health_update", {0: 1, 3: 400.0})          # нас ударили
        regen = []

        def heal_up():
            if not regen and self.clock.t > 1060:
                regen.append(1)
                g.event(1, "regeneration_health_changed", {0: 1, 2: 950.0, 3: 1000.0})
        self.clock.on_sleep.append(heal_up)
        self.assertEqual(self.bot.clear_floor(d, parse_skills("q:1"), self.clock.t), "cleared")
        self.assertIn("атакован — отвечаю", self.texts())
        self.assertNotIn(500, g.mobs)
        self.assertTrue(any("восстанавливается" in t for t in self.texts()))

    def test_bag_full_and_overload(self):
        g = self.game
        g.items_per_loot = 3
        g.add_loot(1, 600, 5, 5)
        self.bot.items_base = self.bot.feed.items_put
        d = self.d(bag_slots=3, explore_min=0.2)
        self.assertEqual(self.bot.clear_floor(d, [], self.clock.t), "bag")
        self.assertIn("сумка полна — домой", self.texts())
        self.bot.items_base = self.bot.feed.items_put
        g.event(1, "overload_mode_update", {0: 1, 1: True})
        self.assertTrue(self.bot.bag_full(self.d()))
        g.event(1, "overload_mode_update", {0: 99, 1: False})  # чужой — не про нас
        self.assertTrue(self.bot.feed.overloaded)

    def test_low_hp_quick_exit(self):
        g = self.game
        g.exit_map = {"DNG1": ("ROAD", (24.0, 30.0))}
        d = self.d(exit_key="a", retreat_exit=True, retreat_hp=30)
        self.mgr.command({"action": "configure", "dungeon": {"exit_key": "a", "retreat_exit": True, "retreat_hp": 30}})
        g.event(1, "regeneration_health_changed", {0: 1, 2: 100.0, 3: 1000.0})
        self.assertEqual(self.bot.clear_dungeon(d, []), "hp")
        self.bot.exit_dungeon(d)
        self.assertEqual(g.zone[1], "ROAD")

    def test_escape_in_open_world(self):
        g = self.game
        g.join(1, "Hero", "ROAD", (0.0, 0.0))
        g.add_player(1, 700, 0, 10, zone="ROAD", name="Gank", faction=255)
        d = self.d(escape_key="3", mount_key="a")
        self.assertTrue(self.bot.avoid_players(d))
        self.assertIn((1, "3"), self.desk.keys)
        self.assertIn((1, "a"), self.desk.keys)
        self.assertLess(g.pos[1][1], -25)                       # убежал далеко
        self.assertTrue(any(t == "Бот: опасные игроки" for _k, t, _x in self.mgr.alerts))
        self.assertFalse(self.bot.avoid_players(self.d(friends="gank")))

    def test_explore_remembers_blocked_directions(self):
        self.game.ignore_clicks = True
        visited = set()
        self.bot.explore(visited)
        self.assertEqual(len(self.bot.explore_failed), 1)


class DungeonServicesTest(DungeonRunTest):
    def test_portal_enchant_filter(self):
        self.game.join(1, "Hero", "ROAD", (-46.0, 0.0))
        d = self.mgr.task_config("dungeon")
        self.assertIsNotNone(self.bot.pick_portal(d))
        self.assertIsNone(self.bot.pick_portal({**d, "portal_enchant_min": 1}))

    def test_bag_full_goes_home_and_services_run(self):
        self.game.items_per_loot = 5
        self.mgr.config["places"]["кузня"] = {"zone": "CITYA", "x": 6.0, "y": 0.0}
        self.mgr.config["macros"]["ремонт"] = "click 0.61 0.43\nexpect 2000"
        self.mgr.command({"action": "configure", "dungeon": {"bag_slots": 5, "runs": 1, "repair_every": 1,
                                                             "repair_place": "кузня", "repair_macro": "ремонт"}})
        self.bot.run("dungeon_run")
        texts = self.texts()
        self.assertIn("сумка полна — несу добычу домой", texts)
        self.assertIn("добыча сдана в сундук", texts)
        self.assertEqual(self.bot.runs, 1, texts[-5:])         # второй заход дошёл до конца
        self.assertIn("ремонт: готово", texts)


class GatherPlusTest(Base):
    def test_heat_map_leads_to_resources_and_respawn_is_respected(self):
        self.window(zone="0201")
        self.bot.calibrate()
        self.bot.home, self.bot.home_zone = (0.0, 0.0), "0201"
        self.mgr.heat = lambda zone: [
            {"x": 45.0, "y": 5.0, "res": "ore", "tier": 4, "enchant": 0, "seen": 9, "last": 0},
            {"x": -40.0, "y": 0.0, "res": "wood", "tier": 4, "enchant": 0, "seen": 50, "last": 0},
            {"x": 400.0, "y": 0.0, "res": "ore", "tier": 4, "enchant": 0, "seen": 99, "last": 0}]
        g = {**DEFAULTS["gather"], "res": ["ore"], "radius": 80}
        cell = self.bot.heat_target(g)
        self.assertEqual((cell["x"], cell["y"]), (45.0, 5.0))           # руда в радиусе, не дерево и не далёкая
        self.bot.heat_visited["0201"] = {(45, 5): self.clock.t}
        self.assertIsNone(self.bot.heat_target(g))                       # недавно были — узлы не восстановились
        self.clock.t += 11 * 60
        self.assertIsNotNone(self.bot.heat_target(g))
        self.assertIsNone(self.bot.heat_target({**g, "use_heat": False}))
        # В работе: рядом пусто → идёт к клетке тепловой карты.
        self.bot.heat_visited.clear()
        closest = []
        self.clock.on_sleep.append(lambda: closest.append(math.hypot(self.game.pos[1][0] - 45,
                                                                     self.game.pos[1][1] - 5)))
        self.clock.limit = self.clock.t + 60
        with self.assertRaises(BotStopped):
            self.bot.gather_session(self.clock.t + 100)
        self.assertLess(min(closest), 4)
        self.assertIn((45, 5), self.bot.heat_visited["0201"])

    def test_prefers_node_in_a_cluster(self):
        self.window()
        g = {**DEFAULTS["gather"], "res": ["ore"]}
        self.game.add_node(1, 1, 10, 0)                                   # одиночка ближе
        for i, (x, y) in enumerate(((-13, 0), (-15, 3), (-16, -3))):      # группа чуть дальше
            self.game.add_node(1, 10 + i, x, y)
        self.assertIn(self.bot.pick_node(g).id, (10, 11, 12))

    def test_bag_full_unloads_and_returns(self):
        self.window(zone="CITYA", pos=(20.0, 20.0))
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.mgr.config["places"] = {"склад": {"zone": "CITYA", "x": 0.0, "y": -20.0}}
        self.game.items_per_harvest = 2
        for i in range(3):
            self.game.add_node(1, 900 + i, 30 + 5 * i, 22)
        self.mgr.command({"action": "configure", "gather": {"res": ["ore"], "bag_slots": 4, "home_place": "склад",
                                                            "radius": 60}})
        self.mgr.config["rest_min"] = 0
        self.clock.limit = self.clock.t + 500
        self.bot.run("gather")
        texts = self.texts()
        self.assertIn("сумка полна — несу домой", texts)
        self.assertIn("добыча сдана в сундук", texts)
        self.assertEqual(self.bot.gathered, 3)
        self.mgr.command({"action": "configure", "gather": {"home_place": ""}})
        self.bot.items_base = -100
        with self.assertRaisesRegex(BotError, "место сдачи не задано"):
            self.bot.unload_gathering(self.mgr.task_config("gather"))


class MarketPlusTest(Base):
    prices = {("T4_BAG", "sell"): 2500, ("T4_BAG", "buy"): 1800}

    def setUp(self):
        super().setUp()
        self.window("Trader", zone="CITYA")
        self.mgr.config["points"] = dict(ALL_POINTS)
        self.mine = []
        self.mgr.my_orders = lambda item, loc, side: self.mine

    def test_own_orders_are_not_duplicated(self):
        m = {**DEFAULTS["market"]}
        self.mine = [{"price": 2490, "outbid": False}]
        self.assertFalse(self.bot.place_order("T4_BAG", "sell", 10, 1, "", m))
        self.assertIn("ваш заказ уже лучший", self.texts()[-1])
        self.mine = [{"price": 2600, "outbid": True}]
        self.assertFalse(self.bot.place_order("T4_BAG", "sell", 10, 1, "", m))
        self.assertIn("переставление выключено", self.texts()[-1])
        self.assertTrue(self.bot.place_order("T4_BAG", "sell", 10, 1, "", {**m, "relist_outbid": True}))
        self.assertIn("перебит — ставлю новый", self.texts()[-2])
        self.mine = [{"price": 2490, "outbid": False}]
        self.assertTrue(self.bot.place_order("T4_BAG", "sell", 10, 1, "", {**m, "skip_own": False}))

    def test_buy_budget(self):
        m = {**DEFAULTS["market"], "budget": 4000}
        self.assertTrue(self.bot.place_order("T4_BAG", "buy", 1, 2, "", m))       # 1801 × 2 = 3602
        self.assertEqual(self.bot.spent, 3602)
        with self.assertRaisesRegex(BotError, "бюджет покупок исчерпан"):
            self.bot.place_order("T4_BAG", "buy", 1, 1, "", m)


REROUTE_INDEX = {
    "CITYA": {"name": "Город А", "type": "PLAYERCITY_SAFEAREA_01", "exits": [[50, 0, "ROAD", "x"]]},
    "ROAD": {"name": "Дорога", "type": "OPENPVP_YELLOW",
             "exits": [[-50, 0, "CITYA", "x"], [0, 50, "CITYB", "x"], [50, 0, "CITYB", "x"]]},
    "CITYB": {"name": "Город Б", "type": "PLAYERCITY_SAFEAREA_02",
              "exits": [[0, -50, "ROAD", "x"], [-50, 0, "ROAD", "x"]]},
}


class TransportRerouteTest(Base):
    index = REROUTE_INDEX

    def test_players_on_the_way_force_another_exit(self):
        self.window("Carrier", zone="CITYA", pos=(0.0, 0.0))
        self.bot.calibrate()
        self.game.add_player(1, 77, -20, 32, zone="ROAD", name="Robber")
        self.bot.route_guard = {**DEFAULTS["transport"], "player_radius": 20}
        self.bot.travel_to("CITYB", 0.0, 0.0, safety="yellow")
        self.assertEqual(self.game.zone[1], "CITYB")
        texts = self.texts()
        self.assertTrue(any("на пути игроки: Robber" in t for t in texts), texts)
        self.assertIn(("ROAD", 0, 50), self.bot.avoid_exits)
        self.assertTrue(any(t == "Бот: игроки на пути" for _k, t, _x in self.mgr.alerts))

    def test_router_avoid_options(self):
        from albion_trader.bot_nav import Router
        r = Router(REROUTE_INDEX)
        self.assertEqual(r.route("ROAD", "CITYB", "yellow", (0, 40))[0][1:3], (0.0, 50.0))
        self.assertEqual(r.route("ROAD", "CITYB", "yellow", (0, 40), avoid_exits=[("ROAD", 0, 50)])[0][1:3],
                         (50.0, 0.0))
        with self.assertRaises(ValueError):
            r.route("CITYA", "CITYB", "yellow", avoid_zones=["ROAD"])


# --- команды и запись -------------------------------------------------------------
class CommandTest(Base):
    def test_settings_saved_and_reloaded(self):
        self.mgr.start_loop = lambda: None
        self.mgr.command({"action": "settings", "enabled": True, "stop_key": "F11", "user_idle": 99,
                          "pause_when_active": False, "work_min": 10, "input": "background"})
        for bad, msg in (({"stop_key": "nope"}, "неизвестная клавиша"), ({"input": "x"}, "режим ввода"),
                         ({"task": "x"}, "неизвестная задача")):
            with self.assertRaisesRegex(BotError, msg):
                self.mgr.command({"action": "settings", **bad})
        again = BotManager(Path(self.tmp.name) / "bots.json", make_radar=Radar)
        self.assertEqual((again.config["stop_key"], again.config["user_idle"], again.config["pause_when_active"],
                          again.config["work_min"], again.config["input"]), ("f11", 60.0, False, 10.0, "background"))
        self.mgr.command({"action": "settings", "enabled": False})
        self.assertTrue(self.bot.stop_event.is_set())

    def test_corrupt_config_and_unknown_keys(self):
        p = Path(self.tmp.name) / "bad.json"
        p.write_text("{oops")
        with self.assertLogs("albion_trader.bots", "WARNING"):
            self.assertEqual(BotManager(p, make_radar=Radar).config["stop_key"], "f12")
        p.write_text(json.dumps({"gather": {"tier_min": 5, "junk": 1}, "places": [], "zzz": 1}))
        cfg = BotManager(p, make_radar=Radar).config
        self.assertEqual((cfg["gather"]["tier_min"], cfg["places"]), (5, {}))
        self.assertNotIn("junk", cfg["gather"])
        self.assertNotIn("zzz", cfg)

    def test_commands_validate(self):
        with self.assertRaisesRegex(BotError, "неизвестная команда"):
            self.mgr.command({"action": "fly"})
        with self.assertRaisesRegex(BotError, "имя макроса"):
            self.mgr.command({"action": "save_macro", "name": ""})
        with self.assertRaisesRegex(BotError, "строка 1"):
            self.mgr.command({"action": "save_macro", "name": "x", "text": "zap"})
        with self.assertRaisesRegex(BotError, "неизвестная точка"):
            self.mgr.command({"action": "capture_point", "name": "zzz"})
        with self.assertRaisesRegex(BotError, "название места"):
            self.mgr.command({"action": "save_place", "name": " "})
        cfg = self.mgr.command({"action": "configure", "gather": {"tier_min": 6, "junk": 1}})["config"]
        self.assertEqual(cfg["gather"]["tier_min"], 6)
        self.assertNotIn("junk", cfg["gather"])

    def test_places_points_and_macro_recording(self):
        self.window(zone="CITYA", pos=(3.0, 4.0))
        self.mgr.command({"action": "save_place", "name": "банк"})
        self.assertIn("Город А", self.mgr.message)
        snap = self.mgr.snapshot()
        self.assertEqual(snap["places"], [{"name": "банк", "zone": "CITYA", "x": 3.0, "y": 4.0, "zone_name": "Город А"}])
        self.mgr.command({"action": "delete_place", "name": "банк"})
        self.assertEqual(self.mgr.snapshot()["places"], [])
        # Точка интерфейса — первый клик в окне игры.
        win = self.game.windows[1]
        self.mgr.command({"action": "capture_point", "name": "search"})
        self.assertEqual(self.mgr.snapshot()["recording"]["point"], "search")
        self.desk.fg, self.desk.cur, self.desk.pressed = win.hwnd, (800, 450), {VK_LBUTTON}
        self.mgr.tick()
        self.assertIsNone(self.mgr.recorder)
        self.assertEqual(self.mgr.config["points"]["search"], [0.5003, 0.5006])
        self.assertEqual(self.mgr.config["points_size"], "1600x900")
        self.assertIn("Поле поиска", self.mgr.message)
        self.mgr.command({"action": "delete_point", "name": "search"})
        self.assertNotIn("search", self.mgr.config["points"])
        self.mgr.command({"action": "capture_point", "name": "search"})
        self.mgr.command({"action": "cancel_capture"})
        self.assertIsNone(self.mgr.recorder)
        # Макрос целиком.
        self.desk.pressed = set()
        self.mgr.command({"action": "record_start"})
        self.desk.pressed = {VK_LBUTTON}
        self.mgr.tick()
        out = self.mgr.command({"action": "record_stop"})
        self.assertEqual(out["text"], "click 0.5003 0.5006")
        self.mgr.command({"action": "save_macro", "name": "m", "text": out["text"]})
        self.assertIn("m", self.mgr.snapshot()["macros"])
        self.mgr.command({"action": "delete_macro", "name": "m"})
        self.assertNotIn("m", self.mgr.snapshot()["macros"])

    def test_recorder_details(self):
        self.window()
        win = self.game.windows[1]
        rec = Recorder(1, self.clock)
        self.desk.fg, self.desk.cur, self.desk.pressed = win.hwnd, (800, 450), {VK_LBUTTON}
        rec.poll(self.desk, win)
        rec.poll(self.desk, win)          # кнопку держат — второй клик не пишется
        self.desk.pressed = set()
        rec.poll(self.desk, win)
        self.clock.t += 1.2
        self.desk.cur, self.desk.pressed = (1599, 0), {0x02}
        rec.poll(self.desk, win)
        self.desk.pressed = set()
        rec.poll(self.desk, win)
        self.desk.fg, self.desk.pressed = 5, {VK_LBUTTON}       # клик в другом окне не пишется
        rec.poll(self.desk, win)
        rec.poll(self.desk, None)
        self.assertEqual(rec.text(), "click 0.5003 0.5006\nwait 1200\nrclick 1.0000 0.0000")

    def test_snapshot_shape(self):
        self.window()
        self.game.add_mob(1, 1, 5, 5)
        snap = self.mgr.snapshot()
        self.assertEqual(snap["game"]["counts"]["mob"], 1)
        self.assertEqual(set(snap["tasks_config"]), set(DEFAULTS))
        self.assertEqual(len(snap["points"]), len(POINTS))
        self.assertIn("market_sell", snap["templates"])
        self.assertEqual(snap["bot"]["stats"]["kills"], 0)
        self.assertTrue(bots_mod.TASKS["transport"])


if __name__ == "__main__":
    unittest.main()
