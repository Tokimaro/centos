"""Тесты ботов на модели игры: клик в окне двигает персонажа, а программа видит
это так же, как в жизни, — через пакеты Photon этого окна (свой UDP-порт)."""

import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from albion_trader import bots as bots_mod
from albion_trader.bot_win import VK, VK_LBUTTON, GameWindow
from albion_trader.bots import (BotError, BotManager, Calibration, ClientFeed, Recorder, fill, market_price,
                                parse_macro, solve_calibration)
from albion_trader.capture.albion import DEFAULT_EVENTS, DEFAULT_OPCODES
from albion_trader.radar import Radar

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb

TRUE_M = [[14.0, 20.0], [16.0, -23.0]]     # «настоящая» камера модели (отличается от умолчания)


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
        self.clicks = []
        self.typed = []
        self.keys = []
        self.focused = []
        self.fg = 1
        self.pressed = set()
        self.cur = (0, 0)
        self.idle = 1e9
        self.alive = True
        self.focus_ok = True
        self.moved_cursor = []

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
        self.game.request(win.pid, 99, {})


class FakeGame:
    """Модель игры: персонажи в окнах, ресурсы, клики → движение и сбор."""

    def __init__(self):
        self.manager = None
        self.windows = {}
        self.pos = {}
        self.port = {}
        self.nodes = {}           # id → (x, y)
        self.harvest_ok = True
        self.ignore_clicks = False
        self.center = (0.5, 0.5)  # где персонаж на экране на самом деле

    def add_window(self, pid, name, pos=(0.0, 0.0), zone="0201", rect=(0, 0, 1600, 900)):
        port = 50000 + pid
        self.windows[pid] = GameWindow(pid * 10, pid, "Albion Online", "Albion-Online.exe", rect, [port])
        self.port[pid] = port
        self.pos[pid] = pos
        self.manager.refresh()
        self.send(pid, pb.response(DEFAULT_OPCODES["join"], {0: pid, 2: name, 8: zone, 9: [float(pos[0]), float(pos[1])]}))

    def send(self, pid, command):
        self.manager.on_packet(self.port[pid], pb.packet(command))

    def request(self, pid, op, params):
        self.send(pid, pb.request(op, params))

    def move_to(self, pid, x, y):
        self.pos[pid] = (x, y)
        self.request(pid, DEFAULT_OPCODES["move"], {1: [float(x), float(y)]})

    def add_node(self, pid, nid, x, y, tier=4, type_id=24, size=3, enchant=0):
        self.nodes[nid] = (x, y)
        self.send(pid, pb.event(DEFAULT_EVENTS["new_harvestable_object"],
                                {0: nid, 5: type_id, 7: tier, 8: [float(x), float(y)], 10: size, 11: enchant}))

    def click(self, pid, fx, fy, button):
        if self.ignore_clicks:
            return
        w = self.windows[pid]
        aspect = w.rect[2] / w.rect[3]
        sx, sy = (fx - self.center[0]) * aspect, fy - self.center[1]
        # Перспектива: верх экрана «дальше» — чуть больше метров на долю.
        k = 1.0 - 0.25 * sy
        dx = (TRUE_M[0][0] * sx + TRUE_M[0][1] * sy) * k
        dy = (TRUE_M[1][0] * sx + TRUE_M[1][1] * sy) * k
        x0, y0 = self.pos[pid]
        tx, ty = x0 + dx, y0 + dy
        for nid, (nx, ny) in list(self.nodes.items()):
            if math.hypot(nx - tx, ny - ty) < 1.5 and math.hypot(nx - x0, ny - y0) < 6:
                self.move_to(pid, nx + 1, ny)
                if self.harvest_ok:
                    self.send(pid, pb.event(DEFAULT_EVENTS["harvestable_change_state"], {0: nid, 1: 1}))
                    self.send(pid, pb.event(DEFAULT_EVENTS["harvest_finished"], {0: pid}))
                    self.send(pid, pb.event(DEFAULT_EVENTS["harvestable_change_state"], {0: nid, 1: 0}))
                    del self.nodes[nid]
                return
        self.move_to(pid, tx, ty)


def make(tmp, prices=None):
    clock = Clock()
    game = FakeGame()
    desk = FakeDesktop(game)
    prices = prices or {}
    mgr = BotManager(Path(tmp) / "bots.json", make_radar=lambda: Radar(clock=clock), desktop=desk,
                     price_of=lambda item, loc, side: prices.get((item, side)),
                     item_name=lambda i: {"T4_BAG": "Сумка адепта"}.get(i, i),
                     clock=clock, sleep=clock.sleep)
    mgr.config["enabled"] = True
    game.manager = mgr
    return mgr, game, desk, clock


class GeometryTest(unittest.TestCase):
    def test_round_trip_and_clamp(self):
        cal = Calibration()
        for w in ((10, 0), (0, -7), (3.5, 12)):
            s = cal.to_screen(*w)
            back = cal.to_world(*s)
            self.assertAlmostEqual(back[0], w[0], places=6)
            self.assertAlmostEqual(back[1], w[1], places=6)
        sx, sy = cal.clamp_step(5, 0, 16 / 9)
        self.assertAlmostEqual(math.hypot(sx, sy), bots_mod.STEP)
        # Шаг вверх упирается в край окна.
        sx, sy = cal.clamp_step(0, -0.6, 16 / 9, step=1.0)
        self.assertAlmostEqual(sy, bots_mod.EDGE - 0.5)
        fx, fy = cal.fractions(0.16, 0.1, 16 / 9)
        self.assertAlmostEqual(fx, 0.5 + 0.09)
        self.assertAlmostEqual(fy, 0.6)

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
        self.assertEqual((cal.cx, cal.cy), (0.5, 0.5))      # пары симметричны — центр верный
        self.assertAlmostEqual(cal.m[0][0], 14.0)
        self.assertAlmostEqual(cal.m[1][1], -23.0)
        with self.assertRaisesRegex(BotError, "почти не двигался"):
            solve_calibration(d, (0.01, 0), (0, 0), (0, 0.01), (0, 0))
        with self.assertRaisesRegex(BotError, "в одну сторону"):
            solve_calibration(d, (2, 2), (-2, -2), (2.1, 2.0), (-2.1, -2.0))


class MacroTest(unittest.TestCase):
    def test_parse_all_steps(self):
        steps = parse_macro("""# открыть рынок
click 0.5 0.25
rclick 0.1 0.9
type {name}
key ctrl+a
wait 500
expect 3000
""")
        self.assertEqual(steps, [("click", 0.5, 0.25), ("rclick", 0.1, 0.9), ("type", "{name}"),
                                 ("key", "ctrl+a"), ("wait", 500), ("expect", 3000)])

    def test_parse_errors_name_the_line(self):
        for text, msg in (("click 2 0.5", "строка 1: координаты"), ("\nclick x", "строка 2: неверные"),
                          ("key hyper", "строка 1: неизвестная клавиша"), ("jump 1", "неизвестное действие"),
                          ("wait -5", "время от 0")):
            with self.assertRaisesRegex(ValueError, msg):
                parse_macro(text)

    def test_fill_and_price(self):
        self.assertEqual(fill("{name} x{qty} {unknown}", {"name": "Сумка", "qty": 2}), "Сумка x2 {unknown}")
        self.assertEqual(market_price(1000, "sell", 1), 999)
        self.assertEqual(market_price(1000, "buy", 5), 1005)
        self.assertEqual(market_price(1, "sell", 5), 1)
        self.assertIsNone(market_price(None, "sell", 1))


class FeedTest(unittest.TestCase):
    def test_feed_tracks_own_client(self):
        clock = Clock()
        f = ClientFeed(lambda: Radar(clock=clock), clock=clock)
        f.feed(pb.packet(pb.response(DEFAULT_OPCODES["join"], {0: 7, 2: "Bob", 8: "3004", 9: [1.0, 2.0]})))
        f.feed(pb.packet(pb.request(DEFAULT_OPCODES["move"], {1: [5.0, 6.0]})))
        f.feed(pb.packet(pb.event(DEFAULT_EVENTS["new_harvestable_object"], {0: 3, 5: 24, 7: 5, 8: [9.0, 9.0], 10: 4})))
        self.assertEqual(f.character, "Bob")
        self.assertEqual(f.location, "3004")
        self.assertEqual((f.me["x"], f.me["y"]), (5.0, 6.0))
        self.assertEqual(f.requests, 1)
        self.assertEqual(f.request_log[-1][1], "move")
        self.assertEqual([e.res for e in f.entities("resource")], ["ore"])
        self.assertIsNotNone(f.entity(3))
        self.assertEqual(f.radar.pending_nodes, [])


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr, self.game, self.desk, self.clock = make(self.tmp.name)

    def tearDown(self):
        self.mgr.stop_all()
        self.tmp.cleanup()

    def test_windows_traffic_split_by_port(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.add_window(2, "Bob", (100, 100), zone="3004")
        snap = self.mgr.snapshot()
        self.assertEqual([(w["pid"], w["character"], w["zone"]) for w in snap["windows"]],
                         [(1, "Alice", "0201"), (2, "Bob", "3004")])
        self.assertTrue(all(w["traffic"] for w in snap["windows"]))
        self.game.move_to(2, 110, 100)
        self.assertEqual(self.mgr.feed_for(2).me["x"], 110)
        self.assertEqual(self.mgr.feed_for(1).me["x"], 0)

    def test_disabled_ignores_traffic(self):
        self.mgr.config["enabled"] = False
        self.mgr.on_packet(50001, pb.packet(pb.request(21, {1: [1.0, 1.0]})))
        self.assertEqual(self.mgr.feeds, {})

    def test_calibration_finds_camera(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        cal = bot.calibrate()
        self.assertTrue(cal.measured)
        for got, want in zip(sum(cal.m, []), sum(TRUE_M, [])):
            self.assertAlmostEqual(got, want, delta=abs(want) * 0.1)
        self.assertTrue(json.loads((Path(self.tmp.name) / "bots.json").read_text())["bots"]["Alice"]["calib"]["measured"])

    def test_calibration_finds_where_the_character_stands(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.center = (0.47, 0.56)
        cal = self.mgr.bot(1).calibrate()
        self.assertAlmostEqual(cal.cx, 0.47, delta=0.01)
        self.assertAlmostEqual(cal.cy, 0.56, delta=0.015)
        self.game.add_node(1, 801, 2.0, 1.0)
        bot = self.mgr.bot(1)
        self.assertTrue(bot.harvest(self.mgr.feed_for(1).entity(801)))   # клик по узлу точно в цель

    def test_three_failed_nodes_stop_gathering(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.harvest_ok = False
        for i in range(4):
            self.game.add_node(1, 900 + i, 10 + 4 * i, 3)
        bot = self.mgr.bot(1)
        self.clock.limit = self.clock.t + 5000
        bot.run("gather")
        self.assertEqual(bot.status, "ошибка")
        self.assertIn("три узла подряд", bot.log[-1]["text"])

    def test_zone_change_moves_home(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        bot.calibrate()
        bot.home, bot.home_zone = (0, 0), "0201"
        self.game.send(1, pb.response(DEFAULT_OPCODES["join"], {0: 1, 2: "Alice", 8: "0202", 9: [500.0, 500.0]}))
        self.game.pos[1] = (500.0, 500.0)
        self.clock.limit = self.clock.t + 30
        with self.assertRaises(bots_mod.BotStopped):
            bot.gather_session(self.clock.t + 100)
        self.assertEqual((bot.home, bot.home_zone), ((500.0, 500.0), "0202"))
        self.assertTrue(any("сменилась зона (0202)" in x["text"] for x in bot.log))

    def test_calibration_refuses_next_to_resource(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.add_node(1, 1001, 3, 2)
        with self.assertRaisesRegex(BotError, "рядом ресурс"):
            self.mgr.bot(1).calibrate()

    def test_walking_click_avoids_other_nodes(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        bot.calibrate()
        # Прямо на пути — чужой узел там, куда пришёлся бы полный шаг.
        fx, fy, wx, wy = bot.plan_click(40, 0)
        self.game.add_node(1, 1002, wx, wy)
        self.game.add_node(1, 1003, 40, 0)
        bot.click_world(40, 0, keep_clear_of=1003)
        self.assertIn(1002, self.game.nodes)           # не начал собирать чужой узел
        x, y = self.game.pos[1]
        self.assertGreater(math.hypot(x - wx, y - wy), 2.0)

    def test_calibration_fails_when_clicks_do_not_reach_game(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.ignore_clicks = True
        with self.assertRaisesRegex(BotError, "клики не доходят"):
            self.mgr.bot(1).calibrate()

    def test_walk_reaches_far_point_and_detours(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        bot.calibrate()
        self.assertTrue(bot.walk_to(60, -40, tol=2))
        x, y = self.game.pos[1]
        self.assertLess(math.hypot(x - 60, y + 40), 2.0)
        # Без калибровки (умолчание) тоже доходит — короткими шагами с поправкой.
        self.mgr.update_bot("Alice", {"calib": None})
        self.assertTrue(bot.walk_to(0, 0, tol=2.5))

    def test_walk_gives_up_when_stuck(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        bot.calibrate()
        self.game.ignore_clicks = True
        self.assertFalse(bot.walk_to(50, 0))

    def test_gathering_collects_matching_nodes(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.mgr.update_bot("Alice", {"gather": {"res": ["ore"], "tier_min": 4, "radius": 60}, "rest_min": 0})
        self.game.add_node(1, 501, 20, 10, tier=4)              # руда T4 — подходит
        self.game.add_node(1, 502, -15, 5, tier=5, type_id=24)  # руда T5 — подходит
        self.game.add_node(1, 503, 5, 12, tier=4, type_id=1)    # дерево — не наш вид
        self.game.add_node(1, 504, 12, -6, tier=3)              # T3 — ниже порога
        self.game.add_node(1, 505, 200, 0, tier=6)              # слишком далеко
        bot = self.mgr.bot(1)
        self.clock.limit = self.clock.t + 600
        bot.run("gather")
        self.assertEqual(bot.status, "остановлен")
        self.assertEqual(bot.gathered, 2)
        self.assertEqual(set(self.game.nodes), {503, 504, 505})
        texts = [x["text"] for x in bot.log]
        self.assertTrue(any("калибровка готова" in t for t in texts))
        self.assertTrue(any("собран ore T4" in t for t in texts))

    def test_failed_harvest_is_skipped(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.game.harvest_ok = False
        self.game.add_node(1, 601, 8, 8, tier=4)
        bot = self.mgr.bot(1)
        bot.calibrate()
        bot.home = (0, 0)
        node = self.mgr.feed_for(1).entity(601)
        self.assertFalse(bot.harvest(node))
        self.assertIn(601, bot.failed)
        self.assertIsNone(bot.pick_node(bot.cfg["gather"]))
        self.assertTrue(any("сбор не идёт" in x["text"] for x in bot.log))

    def test_hostile_player_and_mobs_are_avoided(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        g = {**bot.cfg["gather"], "avoid_mobs": 10}
        self.game.add_node(1, 701, 10, 0)
        self.game.send(1, pb.event(DEFAULT_EVENTS["new_mob"], {0: 900, 1: 5, 7: [12.0, 0.0]}))
        self.assertIsNone(bot.pick_node(g))
        self.assertIsNotNone(bot.pick_node({**g, "avoid_mobs": 0}))
        self.assertEqual(bot.danger(g), "")
        self.game.send(1, pb.event(DEFAULT_EVENTS["new_character"], {0: 901, 1: "Gank", 12: [20.0, 0.0], 53: 255}))
        self.assertEqual(bot.danger(g), "Gank")
        self.assertEqual(bot.danger({**g, "avoid_players": False}), "")

    def test_stop_key_and_window_close_stop_bots(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        started = threading.Event()
        bot.thread = threading.Thread(target=lambda: (started.set(), bot.stop_event.wait(5)))
        bot.thread.start()
        started.wait(1)
        self.desk.pressed.add(VK["f12"])
        self.mgr.tick()
        self.assertTrue(bot.stop_event.is_set())
        self.assertIn("F12", self.mgr.message)
        bot.thread.join(1)
        bot.stop_event.clear()
        del self.game.windows[1]
        self.mgr.refresh()
        self.assertTrue(bot.stop_event.is_set())

    def test_waits_while_user_is_active_and_restores_focus(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        self.desk.idle = 0.5
        statuses = []

        def user_leaves():
            statuses.append(bot.status)
            if self.clock.t > 1003:
                self.desk.idle = 1e9
        self.clock.on_sleep.append(user_leaves)
        self.desk.fg, self.desk.cur = 777, (5, 6)
        bot.click_at(0.5, 0.6)
        self.assertIn("пауза: вы за компьютером", statuses)
        self.assertEqual(len(self.desk.clicks), 1)
        self.assertEqual(self.desk.focused, [10, 777])
        self.assertEqual(self.desk.moved_cursor, [(5, 6)])

    def test_focus_refused_and_window_gone(self):
        self.game.add_window(1, "Alice", (0, 0))
        bot = self.mgr.bot(1)
        self.desk.focus_ok = False
        with self.assertRaisesRegex(BotError, "переключиться"):
            bot.click_at(0.5, 0.5)
        self.desk.alive = False
        with self.assertRaisesRegex(BotError, "закрыто"):
            bot.click_at(0.5, 0.5)

    def test_background_input_skips_focus(self):
        self.game.add_window(1, "Alice", (0, 0))
        self.mgr.update_bot("Alice", {"input": "background"})
        self.mgr.bot(1).click_at(0.5, 0.5)
        self.assertEqual(self.desk.focused, [])
        self.assertTrue(self.desk.clicks[0][4])

    def test_recorder(self):
        self.game.add_window(1, "Alice", (0, 0))
        win = self.game.windows[1]
        rec = Recorder(1, self.clock)
        self.desk.fg = win.hwnd
        self.desk.cur = (800, 450)
        self.desk.pressed = {VK_LBUTTON}
        rec.poll(self.desk, win)
        rec.poll(self.desk, win)          # кнопку держат — второй клик не пишется
        self.desk.pressed = set()
        rec.poll(self.desk, win)
        self.clock.t += 1.2
        self.desk.cur = (1599, 0)
        self.desk.pressed = {0x02}
        rec.poll(self.desk, win)
        self.desk.pressed = set()
        rec.poll(self.desk, win)
        self.desk.fg = 5                   # клик в другом окне не пишется
        self.desk.pressed = {VK_LBUTTON}
        rec.poll(self.desk, win)
        self.assertEqual(rec.text(), "click 0.5003 0.5006\nwait 1200\nrclick 1.0000 0.0000")


class MarketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr, self.game, self.desk, self.clock = make(
            self.tmp.name, prices={("T4_BAG", "sell"): 2500, ("T4_BAG", "buy"): 1800})
        self.game.add_window(1, "Trader", (0, 0), zone="3004")
        self.mgr.command({"action": "save_macro", "name": "open", "text": "click 0.5 0.4\nwait 500"})
        self.mgr.command({"action": "save_macro", "name": "order",
                          "text": "click 0.2 0.1\ntype {name}\nkey enter\ntype {price}\nexpect 2000"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_market_session_places_orders(self):
        self.mgr.command({"action": "configure", "pid": 1, "task": "market", "rest_min": 0,
                          "market": {"items": "T4_BAG", "side": "sell", "undercut": 10,
                                     "interval_min": 1, "interval_max": 1,
                                     "macro_open": "open", "macro_order": "order"}})
        bot = self.mgr.bot(1)
        self.clock.limit = self.clock.t + 150
        bot.run("market")
        self.assertGreaterEqual(bot.orders, 2)
        self.assertIn((1, "Сумка адепта"), self.desk.typed)
        self.assertIn((1, "2490"), self.desk.typed)
        self.assertEqual(self.desk.keys[0], (1, "enter"))
        self.assertTrue(any("продажа: Сумка адепта × 1 по 2490" in x["text"] for x in bot.log))

    def test_buy_side_and_missing_price(self):
        self.mgr.update_bot("Trader", {"market": {"items": "T4_BAG, T5_BAG", "side": "buy", "undercut": 1,
                                                  "macro_order": "order"}})
        bot = self.mgr.bot(1)
        bot.rng.seed(3)
        self.clock.limit = self.clock.t + 3000
        bot.run("market")
        self.assertIn((1, "1801"), self.desk.typed)
        self.assertTrue(any("T5_BAG: нет цены" in x["text"] for x in bot.log))

    def test_expect_fails_when_game_is_silent(self):
        self.mgr.command({"action": "save_macro", "name": "silent", "text": "click 0.5 0.5\nwait 100\nexpect 1000"})
        bot = self.mgr.bot(1)
        self.mgr.command({"action": "save_macro", "name": "ok", "text": "click 0.5 0.6\nwait 300\nexpect 1000"})
        bot.run_macro("ok", {})           # запрос пришёл сразу после клика, до ожидания
        self.game.ignore_clicks = True
        with self.assertRaisesRegex(BotError, "не отправила запрос"):
            bot.run_macro("silent", {})

    def test_market_errors(self):
        bot = self.mgr.bot(1)
        for market, msg in (({"items": ""}, "не задан список"), ({"items": "T4_BAG", "macro_order": ""}, "не выбран макрос"),
                            ({"items": "T4_BAG", "macro_order": "nope"}, "нет макроса")):
            self.mgr.update_bot("Trader", {"market": market})
            bot.run("market")
            self.assertEqual(bot.status, "ошибка")
            self.assertIn(msg, bot.log[-1]["text"])
        self.mgr.config["macros"]["bad"] = "click 5 5"
        with self.assertRaisesRegex(BotError, "макрос «bad», строка 1"):
            bot.run_macro("bad", {})


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr, self.game, self.desk, self.clock = make(self.tmp.name)

    def tearDown(self):
        self.mgr.stop_all()
        self.tmp.cleanup()

    def test_settings_saved_and_reloaded(self):
        self.mgr.start_loop = lambda: None
        self.mgr.command({"action": "settings", "enabled": True, "stop_key": "F11", "user_idle": 99,
                          "pause_when_active": False})
        with self.assertRaisesRegex(BotError, "неизвестная клавиша"):
            self.mgr.command({"action": "settings", "stop_key": "nope"})
        again = BotManager(Path(self.tmp.name) / "bots.json", make_radar=Radar)
        self.assertEqual((again.config["stop_key"], again.config["user_idle"], again.config["pause_when_active"]),
                         ("f11", 60.0, False))

    def test_corrupt_config_falls_back(self):
        p = Path(self.tmp.name) / "bad.json"
        p.write_text("{oops")
        with self.assertLogs("albion_trader.bots", "WARNING"):
            self.assertEqual(BotManager(p, make_radar=Radar).config["stop_key"], "f12")

    def test_commands_validate(self):
        with self.assertRaisesRegex(BotError, "не указано окно"):
            self.mgr.command({"action": "start"})
        with self.assertRaisesRegex(BotError, "не найдено"):
            self.mgr.command({"action": "start", "pid": 5})
        with self.assertRaisesRegex(BotError, "имя макроса"):
            self.mgr.command({"action": "save_macro", "name": ""})
        with self.assertRaisesRegex(BotError, "строка 1"):
            self.mgr.command({"action": "save_macro", "name": "x", "text": "zap"})
        with self.assertRaisesRegex(BotError, "неизвестная команда"):
            self.mgr.command({"action": "fly"})
        self.game.windows[3] = GameWindow(30, 3, "Albion Online", rect=(0, 0, 800, 600), ports=[50003])
        self.mgr.refresh()
        with self.assertRaisesRegex(BotError, "персонаж окна ещё не известен"):
            self.mgr.command({"action": "start", "pid": 3})
        with self.assertRaisesRegex(BotError, "персонаж окна ещё не известен"):
            self.mgr.command({"action": "configure", "pid": 3, "task": "gather"})
        self.game.port[3] = 50003
        self.game.send(3, pb.response(DEFAULT_OPCODES["join"], {0: 3, 2: "Cid", 8: "0201", 9: [0.0, 0.0]}))
        with self.assertRaisesRegex(BotError, "режим ввода"):
            self.mgr.command({"action": "configure", "pid": 3, "input": "telepathy"})
        with self.assertRaisesRegex(BotError, "неизвестная задача"):
            self.mgr.command({"action": "configure", "pid": 3, "task": "dance"})
        cfg = self.mgr.command({"action": "configure", "pid": 3, "task": "wander", "work_min": 5,
                                "gather": {"tier_min": 6, "junk": 1}})["config"]
        self.assertEqual((cfg["task"], cfg["work_min"], cfg["gather"]["tier_min"]), ("wander", 5, 6))
        self.assertNotIn("junk", cfg["gather"])

    def test_record_and_macros_via_commands(self):
        self.game.add_window(1, "Alice")
        self.mgr.command({"action": "record_start", "pid": 1})
        self.assertEqual(self.mgr.snapshot()["recording"], 1)
        win = self.game.windows[1]
        self.desk.fg, self.desk.cur, self.desk.pressed = win.hwnd, (0, 0), {VK_LBUTTON}
        self.mgr.tick()
        out = self.mgr.command({"action": "record_stop"})
        self.assertEqual(out["text"], "click 0.0000 0.0000")
        self.mgr.command({"action": "save_macro", "name": "m", "text": out["text"]})
        self.assertIn("m", self.mgr.snapshot()["macros"])
        self.mgr.command({"action": "delete_macro", "name": "m"})
        self.assertNotIn("m", self.mgr.snapshot()["macros"])

    def test_start_stop_threads_and_test_click(self):
        self.game.add_window(1, "Alice")
        self.mgr.command({"action": "start", "pid": 1, "task": "wander"})
        bot = self.mgr.bots[1]
        with self.assertRaisesRegex(BotError, "уже работает"):
            bot.start("wander")
        self.mgr.command({"action": "stop", "pid": 1})
        bot.thread.join(5)
        self.assertFalse(bot.running)
        self.assertEqual(bot.status, "остановлен")
        with self.assertRaisesRegex(BotError, "неизвестная задача"):
            bot.start("dance")
        self.mgr.command({"action": "calibrate", "pid": 1})
        bot.thread.join(5)
        self.assertTrue(self.mgr.bot_config("Alice")["calib"]["measured"])
        self.mgr._test_click(bot, 0.5, 0.7)
        self.assertIn("ответила запросом", bot.log[-1]["text"])
        self.game.ignore_clicks = True
        self.mgr._test_click(bot, 0.5, 0.7)
        self.assertIn("не отправила запрос", bot.log[-1]["text"])
        self.mgr.command({"action": "stop_all"})

    def test_feed_limit_and_snapshot_without_traffic(self):
        for port in range(70000 - 70, 70000):
            self.mgr.on_packet(port, pb.packet(pb.request(21, {1: [1.0, 1.0]})))
        self.assertLessEqual(len(self.mgr.feeds), 65)
        self.game.windows[4] = GameWindow(40, 4, "Albion Online", rect=(0, 0, 800, 600), ports=[1])
        self.mgr.refresh()
        w = self.mgr.snapshot()["windows"][0]
        self.assertEqual((w["character"], w["traffic"], w["pos"]), ("", False, None))
        with self.assertRaisesRegex(BotError, "нет трафика"):
            _ = self.mgr.bot(4).feed


if __name__ == "__main__":
    unittest.main()
