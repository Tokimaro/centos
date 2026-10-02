"""Тесты слоя Windows для ботов на подменённых функциях user32/kernel32/iphlpapi,
перехват пакетов по локальному порту и API /api/bots."""

import ctypes
import json
import shutil
import subprocess
import struct
import tempfile
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from albion_trader import bot_win
from albion_trader.bot_win import (KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
                                   MOUSEEVENTF_RIGHTDOWN, VK, WM_CHAR, WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN,
                                   Desktop, GameWindow, WinApi, lparam, parse_keys, parse_udp_table)
from albion_trader.capture.sniffer import Sniffer
from albion_trader.locations import normalize_location
from albion_trader.server import App, AppConfig, make_handler

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb


def udp_table(rows):
    buf = struct.pack("<I", len(rows))
    for port, pid in rows:
        buf += struct.pack("<III", 0, ((port & 0xFF) << 8) | (port >> 8), pid)
    return buf


class FakeUser32:
    def __init__(self):
        # hwnd → (pid, title, visible)
        self.wins = {11: (100, "Albion Online Client", True), 12: (200, "Albion Online - Microsoft Edge", True),
                     13: (300, "Блокнот", True), 14: (100, "Albion Online hidden", False),
                     15: (400, "Albion Online", True)}
        self.fg = 13
        self.cursor = (5, 5)
        self.sent, self.posted, self.calls = [], [], []
        self.pressed = set()
        self.last_input = 0
        self.focus_ok = True

    def IsWindowVisible(self, h):
        return self.wins[h][2]

    def GetWindowTextLengthW(self, h):
        return len(self.wins[h][1])

    def GetWindowTextW(self, h, buf, n):
        buf.value = self.wins[h][1][:n - 1]

    def GetWindowThreadProcessId(self, h, ref):
        ref._obj.value = self.wins[h][0]

    def EnumWindows(self, cb, _lp):
        for h in self.wins:
            cb(h, 0)

    def GetClientRect(self, h, ref):
        ref._obj.right, ref._obj.bottom = 1600, 900

    def ClientToScreen(self, h, ref):
        ref._obj.x, ref._obj.y = 100, 50

    def IsIconic(self, h):
        return h == 15

    def ShowWindow(self, h, cmd):
        self.calls.append(("show", h, cmd))

    def SetForegroundWindow(self, h):
        self.calls.append(("fg", h))
        if self.focus_ok:
            self.fg = h

    def GetForegroundWindow(self):
        return self.fg

    def SendInput(self, n, arr, size):
        for inp in arr[:n]:
            if inp.type == bot_win.INPUT_MOUSE:
                self.sent.append(("mouse", inp.u.mi.dwFlags))
            else:
                self.sent.append(("key", inp.u.ki.wVk, inp.u.ki.wScan, inp.u.ki.dwFlags))
        return n

    def SetCursorPos(self, x, y):
        self.cursor = (x, y)

    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = self.cursor

    def PostMessageW(self, h, msg, w, l):
        self.posted.append((h, msg, w, l))

    def GetAsyncKeyState(self, vk):
        return 0x8000 if vk in self.pressed else 0

    def IsWindow(self, h):
        return h in self.wins

    def GetLastInputInfo(self, ref):
        ref._obj.dwTime = self.last_input
        return 1


class FakeKernel32:
    exes = {100: "C:\\Games\\Albion Online\\game\\Albion-Online.exe", 200: "C:\\Edge\\msedge.exe",
            300: "C:\\Windows\\notepad.exe"}

    def __init__(self):
        self.ticks = 100_000
        self.closed = []

    def OpenProcess(self, _acc, _inh, pid):
        return pid if pid in self.exes else 0

    def QueryFullProcessImageNameW(self, h, _flags, buf, ref):
        buf.value = self.exes[h]
        return 1

    def CloseHandle(self, h):
        self.closed.append(h)

    def GetTickCount(self):
        return self.ticks


class FakeIphlp:
    def __init__(self, rows):
        self.data = udp_table(rows)
        self.calls = 0

    def GetExtendedUdpTable(self, buf, ref, _order, af, cls, _res):
        self.calls += 1
        assert (af, cls) == (bot_win.AF_INET, bot_win.UDP_TABLE_OWNER_PID)
        if buf is None or len(buf) < len(self.data):
            ref._obj.value = len(self.data)
            return bot_win.ERROR_INSUFFICIENT_BUFFER
        ctypes.memmove(buf, self.data, len(self.data))
        return 0


def desktop(rows=((50001, 100), (50002, 100), (60000, 400), (53, 300))):
    u, k, i = FakeUser32(), FakeKernel32(), FakeIphlp(list(rows))
    return Desktop(WinApi(u, k, i), sleep=lambda _s: None), u, k, i


class HelpersTest(unittest.TestCase):
    def test_parse_udp_table(self):
        self.assertEqual(parse_udp_table(udp_table([(50001, 7), (5056, 9)])), [(50001, 7), (5056, 9)])
        self.assertEqual(parse_udp_table(b""), [])
        self.assertEqual(parse_udp_table(struct.pack("<I", 3) + b"\0" * 12), [(0, 0)])   # обрезанная таблица

    def test_parse_keys(self):
        self.assertEqual(parse_keys("Ctrl + A"), [VK["ctrl"], ord("A")])
        self.assertEqual(parse_keys("f12"), [0x7B])
        with self.assertRaises(ValueError):
            parse_keys("ctrl+hyper")

    def test_window_points(self):
        w = GameWindow(1, 2, "t", rect=(100, 50, 1600, 900))
        self.assertEqual(w.point(0, 0), (100, 50))
        self.assertEqual(w.point(1, 1), (1699, 949))
        self.assertEqual(w.point(2, -1), (1699, 50))     # за краем — к краю
        self.assertEqual(w.local_point(0.5, 0.5), (800, 450))
        self.assertEqual(w.to_dict()["rect"], [100, 50, 1600, 900])
        self.assertEqual(lparam(3, 4), (4 << 16) | 3)

    def test_not_windows(self):
        with mock.patch.object(bot_win, "IS_WINDOWS", False):
            d = Desktop(WinApi())
            self.assertFalse(d.available)
            self.assertEqual(d.game_windows(), [])
            self.assertEqual(d.udp_ports(), {})


class DesktopTest(unittest.TestCase):
    def test_finds_only_game_windows_with_ports(self):
        d, u, k, i = desktop()
        wins = d.game_windows()
        self.assertEqual([(w.hwnd, w.pid, w.exe, w.ports) for w in wins],
                         [(11, 100, "Albion-Online.exe", [50001, 50002]), (15, 400, "", [60000])])
        self.assertEqual(wins[0].rect, (100, 50, 1600, 900))
        self.assertIn(100, k.closed)
        self.assertEqual(i.calls, 2)        # первый вызов узнаёт размер

    def test_udp_table_error(self):
        d, u, k, i = desktop()
        i.GetExtendedUdpTable = lambda *a: 5
        self.assertEqual(d.udp_ports(), {})

    def test_focus_click_and_restore(self):
        d, u, *_ = desktop()
        win = d.game_windows()[0]
        self.assertTrue(d.focus(win.hwnd))
        self.assertEqual(u.fg, 11)
        self.assertEqual(u.sent[:2], [("key", bot_win.VK_MENU, 0, 0), ("key", bot_win.VK_MENU, 0, KEYEVENTF_KEYUP)])
        self.assertTrue(d.focus(win.hwnd))       # уже активно — без нажатий
        self.assertEqual(len(u.sent), 2)
        d.focus(15)
        self.assertIn(("show", 15, bot_win.SW_RESTORE), u.calls)
        u.focus_ok = False
        self.assertFalse(d.focus(13))
        u.sent.clear()
        d.click(win, 0.5, 0.5)
        self.assertEqual(u.cursor, (900, 500))
        self.assertEqual(u.sent, [("mouse", MOUSEEVENTF_LEFTDOWN), ("mouse", MOUSEEVENTF_LEFTUP)])
        u.sent.clear()
        d.click(win, 0, 0, "right")
        self.assertEqual(u.sent[0], ("mouse", MOUSEEVENTF_RIGHTDOWN))
        self.assertEqual(d.cursor(), (100, 50))
        d.move_cursor(7, 8)
        self.assertEqual(u.cursor, (7, 8))
        self.assertEqual(d.foreground(), 15)

    def test_typing_and_keys(self):
        d, u, *_ = desktop()
        win = d.game_windows()[0]
        d.type_text(win, "Я1")
        self.assertEqual(u.sent, [("key", 0, ord("Я"), KEYEVENTF_UNICODE), ("key", 0, ord("Я"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                                  ("key", 0, ord("1"), KEYEVENTF_UNICODE), ("key", 0, ord("1"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        u.sent.clear()
        d.press(win, "ctrl+a")
        self.assertEqual([s[1] for s in u.sent], [VK["ctrl"], ord("A"), ord("A"), VK["ctrl"]])
        self.assertEqual([s[3] for s in u.sent], [0, 0, KEYEVENTF_KEYUP, KEYEVENTF_KEYUP])

    def test_background_messages(self):
        d, u, *_ = desktop()
        win = d.game_windows()[0]
        d.click(win, 0.5, 0.5, background=True)
        self.assertEqual(u.posted[1], (11, WM_LBUTTONDOWN, bot_win.MK_LBUTTON, lparam(800, 450)))
        d.click(win, 0.5, 0.5, "right", background=True)
        self.assertEqual(u.posted[-2][1], bot_win.WM_RBUTTONDOWN)
        u.posted.clear()
        d.type_text(win, "ab", background=True)
        d.press(win, "enter", background=True)
        self.assertEqual(u.posted, [(11, WM_CHAR, ord("a"), 0), (11, WM_CHAR, ord("b"), 0),
                                    (11, WM_KEYDOWN, VK["enter"], 0), (11, WM_KEYUP, VK["enter"], 0)])
        self.assertEqual(u.sent, [])

    def test_idle_ignores_own_input(self):
        d, u, k, _ = desktop()
        k.ticks, u.last_input = 100_000, 98_000        # человек был 2 с назад
        d.own_input_at = time.monotonic() - 100
        self.assertAlmostEqual(d.idle_seconds(), 2.0)
        d.own_input_at = time.monotonic() - 1.9        # это был наш ввод
        self.assertGreater(d.idle_seconds(), 1e8)
        u.GetLastInputInfo = lambda ref: 0
        self.assertGreater(d.idle_seconds(), 1e8)

    def test_keys_and_alive(self):
        d, u, *_ = desktop()
        u.pressed.add(VK["f12"])
        self.assertTrue(d.key_down(VK["f12"]))
        self.assertFalse(d.key_down(VK["f11"]))
        self.assertTrue(d.window_alive(11))
        self.assertFalse(d.window_alive(99))


class SnifferTapTest(unittest.TestCase):
    def test_tap_gets_local_port(self):
        st = mock.Mock(accepts_event=lambda c: True)
        s = Sniffer(st, ports=(5056,), open_sockets=lambda: [])
        got = []
        s.taps.append(lambda port, payload: got.append((port, payload)))
        payload = pb.packet(pb.request(21, {1: [1.0, 2.0]}))
        s.feed_ip_packet(pb.ip_udp(payload, src_port=5056, dst_port=50001))      # входящий: порт получателя
        s.feed_ip_packet(pb.ip_udp(payload, src_port=5056, dst_port=50001))      # дубль с другого адаптера
        s.feed_ip_packet(pb.ip_udp(payload, src_port=50002, dst_port=5056))      # исходящий: порт отправителя
        self.assertEqual([p for p, _ in got], [50001, 50002])
        self.assertEqual(got[0][1], payload)


class BotsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.bots.stop_loop()
        self.tmp.cleanup()

    def post(self, body):
        req = Request(self.url + "/api/bots", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_get_and_post(self):
        with urlopen(self.url + "/api/bots", timeout=5) as r:
            snap = json.loads(r.read())
        self.assertEqual((snap["enabled"], snap["windows"]), (False, []))
        self.assertIn("expect", snap["macro_help"])
        self.assertTrue(self.post({"action": "save_macro", "name": "m", "text": "click 0.1 0.2"})["ok"])
        with self.assertRaises(HTTPError) as e:
            self.post({"action": "save_macro", "name": "m", "text": "nonsense"})
        self.assertEqual(e.exception.code, 400)
        self.assertIn("строка 1", json.loads(e.exception.read())["error"])
        e.exception.close()
        with self.assertRaises(HTTPError) as e:
            self.post({"action": "test_click", "pid": "x"})
        self.assertEqual(e.exception.code, 400)
        e.exception.close()
        self.assertTrue((Path(self.tmp.name) / "bots.json").exists())

    def test_tab_script_served_and_valid(self):
        with urlopen(self.url + "/", timeout=5) as r:
            self.assertIn('src="js/bots.js"', r.read().decode())
        with urlopen(self.url + "/js/bots.js", timeout=5) as r:
            js = r.read().decode()
        self.assertIn('id: "bots"', js)
        node = shutil.which("node")
        if node:
            p = Path(self.tmp.name) / "bots.js"
            p.write_text(js, encoding="utf-8")
            out = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_capture_feeds_bots(self):
        self.app.bots.config["enabled"] = True
        self.app.start_capture(open_sockets=lambda: [])
        self.assertIn(self.app.bots.on_packet, self.app.sniffer.taps)
        self.app.sniffer.feed_ip_packet(pb.ip_udp(pb.packet(pb.request(21, {1: [1.0, 2.0]})),
                                                  src_port=50007, dst_port=5056))
        self.assertIn(50007, self.app.bots.feeds)
        self.app.sniffer.stop()

    def test_bot_price(self):
        now = int(time.time())
        with self.app.conn() as conn:
            for oid, loc, price, kind in ((1, "3005", 900, "offer"), (2, "3005", 800, "offer"),
                                          (3, "3005", 500, "request"), (4, "3005", 600, "request"),
                                          (5, "0007", 1500, "offer")):
                conn.execute("INSERT INTO orders(id, item_id, location, quality, enchant, price, amount, "
                             "auction_type, seen_at) VALUES (?, 'T4_BAG', ?, 1, 0, ?, 1, ?, ?)",
                             (oid, normalize_location(loc), price, kind, now))
        self.assertEqual(self.app.bot_price("T4_BAG", "3005", "sell"), 800)
        self.assertEqual(self.app.bot_price("T4_BAG", "3005", "buy"), 600)
        self.assertIsNone(self.app.bot_price("T4_BAG", "4002", "buy"))
        self.assertIsNotNone(self.app.bot_price("T4_BAG", "", "sell"))     # оценка по всем городам
        self.assertIsNone(self.app.bot_price("T9_NOTHING", "3005", "sell"))


if __name__ == "__main__":
    unittest.main()
