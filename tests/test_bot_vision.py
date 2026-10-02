"""Тесты автоопределения точек: снимок окна (подменённый GDI), PNG, распознавание текста
(подменённый PowerShell), поиск подписей и пробные клики в модели игры."""

import ctypes
import json
import struct
import subprocess
import unittest
import zlib
from pathlib import Path

from albion_trader import bot_vision as bv
from albion_trader.bot_core import BotError
from albion_trader.bot_win import Desktop, GameWindow, WinApi
from albion_trader.capture.albion import DEFAULT_OPCODES

try:
    from . import test_bots as tb
except ImportError:
    import test_bots as tb


def png_pixels(png: bytes):
    """Разбор нашего PNG (фильтр 0) → ширина, высота, RGBA."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        tag, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0]
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF
        if tag == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif tag == b"IDAT":
            idat += data
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 4 + 1) + 1:(y + 1) * (w * 4 + 1)] for y in range(h)]
    return w, h, b"".join(rows)


def word(t, x, y, w=40, h=12):
    return {"t": t, "x": x, "y": y, "w": w, "h": h}


class ImageTest(unittest.TestCase):
    def test_png_roundtrip_and_scale(self):
        bgra = bytes([10, 20, 30, 0, 40, 50, 60, 0, 70, 80, 90, 0, 1, 2, 3, 0])     # 2×2
        w, h, rgba = png_pixels(bv.png_encode(2, 2, bgra))
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(rgba[:8], bytes([30, 20, 10, 255, 60, 50, 40, 255]))
        sw, sh, big = bv.scale2x(2, 2, bgra)
        self.assertEqual((sw, sh, len(big)), (4, 4, 64))
        self.assertEqual(big[:16], bgra[:4] * 2 + bgra[4:8] * 2)            # пиксели удвоены по x
        self.assertEqual(big[16:32], big[:16])                              # и по y
        self.assertTrue(bv.data_url(b"x").startswith("data:image/png;base64,"))


class FindPointsTest(unittest.TestCase):
    LINES = [
        {"text": "Купить Пpодать", "words": [word("Купить", 100, 50), word("Пpодать", 160, 50)]},   # латинская p
        {"text": "Поиск...", "words": [word("Поиск...", 100, 80)]},
        {"text": "Создать заказ на продажу", "words": [word("Создать", 300, 400), word("заказ", 350, 400),
                                                        word("на", 400, 400), word("продажу", 420, 400)]},
        {"text": "Цена за штуку", "words": [word("Цена", 100, 200), word("за", 150, 200), word("штуку", 170, 200)]},
        {"text": "Количество", "words": [word("Количество", 100, 230, 80)]},
        {"text": "Создать заказ", "words": [word("Создать", 300, 500), word("заказ", 350, 500)]},
        {"text": "Взять всё", "words": [word("Взять", 600, 300), word("всё", 650, 300)]},
        {"text": "Случайный текст", "words": [word("Случайный", 10, 10), word("текст", 60, 10)]},
    ]

    def test_finds_buttons_and_fields(self):
        found = {f["name"]: f for f in bv.find_points(self.LINES, 1000, 600)}
        self.assertEqual(set(found), {"sell_tab", "buy_tab", "search", "sell_order", "price", "qty", "confirm",
                                      "loot_all"})
        self.assertAlmostEqual(found["sell_tab"]["x"], 180 / 999, places=3)       # центр надписи
        self.assertGreater(found["sell_tab"]["score"], 0.8)
        so, cf = found["sell_order"], found["confirm"]
        self.assertAlmostEqual(so["y"], 406 / 599, places=3)                       # кнопка заказа — первая строка
        self.assertAlmostEqual(cf["y"], 506 / 599, places=3)                       # подтверждение — отдельная кнопка
        self.assertTrue(found["price"]["approx"])
        self.assertGreater(found["price"]["x"], (170 + 40) / 999)                 # поле правее подписи
        self.assertFalse(found["loot_all"]["approx"])

    def test_scale_and_english(self):
        lines = [{"text": "Take All", "words": [word("Take", 200, 100), word("All", 250, 100)]},
                 {"text": "Deposit All", "words": [word("Deposit", 400, 300), word("All", 470, 300)]}]
        found = {f["name"]: f for f in bv.find_points(lines, 500, 300, scale=2)}
        self.assertAlmostEqual(found["loot_all"]["x"], (245 / 2) / 499, places=3)
        self.assertIn("stash_deposit", found)
        self.assertEqual(bv.find_points([], 10, 10), [])
        self.assertEqual(bv.norm("Взять ВСЁ!"), "взять все")


class OcrTest(unittest.TestCase):
    def run_with(self, stdout="", code=0, stderr="", exc=None):
        def fake(cmd, **kw):
            self.assertTrue(Path(kw["env"]["AT_OCR_IMAGE"]).exists())
            self.assertEqual(cmd[0], "powershell")
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, code, stdout, stderr)
        return fake

    def test_parses_output(self):
        out = json.dumps({"lang": "ru", "lines": {"text": "Взять всё", "words": word("Взять", 1, 2)}})
        res = bv.ocr_windows(b"png", "ru", run=self.run_with("шум\n" + out))
        self.assertEqual(res["lang"], "ru")
        self.assertEqual(res["lines"][0]["words"][0]["t"], "Взять")

    def test_errors(self):
        for kw, msg in (({"code": 1, "stderr": "boom"}, "не сработало: boom"), ({"stdout": ""}, "нет ответа"),
                        ({"stdout": "{bad"}, "непонятный ответ"),
                        ({"stdout": '{"error": "no-ocr-language"}'}, "нет языка"),
                        ({"exc": FileNotFoundError()}, "PowerShell не найден"),
                        ({"exc": subprocess.TimeoutExpired("x", 1)}, "не уложилось")):
            with self.assertRaisesRegex(bv.OcrError, msg):
                bv.ocr_windows(b"png", run=self.run_with(**kw))


class FakeGdi:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def CreateCompatibleDC(self, dc):
        return 2

    def CreateCompatibleBitmap(self, dc, w, h):
        return 3

    def SelectObject(self, dc, obj):
        self.calls.append(("select", obj))
        return 4

    def BitBlt(self, *a):
        self.calls.append(("bitblt", a[1:5], a[6:8]))
        return 1 if self.ok else 0

    def GetDIBits(self, mem, bmp, start, h, buf, info_ref, usage):
        info = info_ref._obj
        self.calls.append(("dib", info.biWidth, info.biHeight, info.biBitCount))
        ctypes.memmove(buf, bytes(range(16)), 16)
        return h

    def DeleteObject(self, o):
        self.calls.append(("delete", o))

    def DeleteDC(self, o):
        self.calls.append(("deletedc", o))


class FakeUser:
    def GetDC(self, h):
        return 1

    def ReleaseDC(self, h, dc):
        self.released = dc


class CaptureTest(unittest.TestCase):
    def test_capture_client_area(self):
        g, u = FakeGdi(), FakeUser()
        d = Desktop(WinApi(user32=u, gdi32=g))
        win = GameWindow(1, 2, "Albion Online", rect=(100, 50, 4, 2))
        w, h, px = d.capture(win)
        self.assertEqual((w, h, len(px)), (4, 2, 32))
        self.assertEqual(px[:16], bytes(range(16)))
        self.assertIn(("bitblt", (0, 0, 4, 2), (100, 50)), g.calls)
        self.assertIn(("dib", 4, -2, 32), g.calls)                 # сверху вниз, 32 бита
        self.assertIn(("delete", 3), g.calls)
        self.assertEqual(u.released, 1)
        with self.assertRaisesRegex(OSError, "не дала снять"):
            Desktop(WinApi(user32=u, gdi32=FakeGdi(ok=False))).capture(win)
        with self.assertRaisesRegex(OSError, "свёрнуто"):
            d.capture(GameWindow(1, 2, "x", rect=(0, 0, 0, 0)))


class AutoPointsTest(tb.Base):
    def setUp(self):
        super().setUp()
        self.game.add_window(1, "Trader", (0.0, 0.0), zone="CITYA", rect=(0, 0, 320, 180))
        self.bot.pid = 1
        self.desk.capture = lambda win: (win.rect[2], win.rect[3], bytes(win.rect[2] * win.rect[3] * 4))
        self.ocr_calls = []

        def ocr(png, lang):
            self.ocr_calls.append(len(png))
            return {"lang": "ru-RU", "lines": [{"text": "Взять всё", "words": [word("Взять", 200, 100), word("всё", 250, 100)]}]}
        self.mgr.ocr = ocr

    def test_snapshot_finds_and_applies(self):
        out = self.mgr.command({"action": "snapshot"})
        self.assertEqual([f["name"] for f in out["found"]], ["loot_all"])
        v = self.mgr.vision
        self.assertEqual(v["size"], "320x180")
        self.assertTrue(v["image"].startswith("data:image/png"))
        self.assertEqual(v["lang"], "ru-RU")
        f = out["found"][0]
        self.assertAlmostEqual(f["x"], (245 / 2) / 319, places=3)      # распознавание шло по увеличенному снимку
        self.assertIn("найдено — Кнопка «Взять всё»", self.mgr.message)
        self.assertEqual(self.desk.focused[0], 10)
        self.mgr.command({"action": "apply_points", "points": {"loot_all": [f["x"], f["y"]]}})
        self.assertEqual(self.mgr.config["points"]["loot_all"], [f["x"], f["y"]])
        self.assertEqual(self.mgr.config["points_size"], "320x180")
        self.assertEqual(self.mgr.snapshot()["auto_points"]["probe"], ["market_npc", "stash_open"])

    def test_snapshot_errors(self):
        self.mgr.ocr = lambda png, lang: (_ for _ in ()).throw(bv.OcrError("нет языка"))
        out = self.mgr.command({"action": "snapshot"})
        self.assertEqual(out["error"], "нет языка")
        self.assertTrue(self.mgr.vision["image"])                       # снимок есть — точки можно поставить вручную
        self.desk.capture = lambda win: (_ for _ in ()).throw(OSError("чёрный экран"))
        with self.assertRaisesRegex(BotError, "не удалось снять окно игры: чёрный экран"):
            self.mgr.command({"action": "snapshot"})
        self.desk.focus_ok = False
        with self.assertRaisesRegex(BotError, "не дала показать"):
            self.mgr.command({"action": "snapshot"})
        for points, msg in (({}, "нет точек"), ({"zzz": [0, 0]}, "неизвестная точка"), ({"search": [2, 0]}, "от 0 до 1"),
                            ({"search": "x"}, "две доли")):
            with self.assertRaisesRegex(BotError, msg):
                self.mgr.command({"action": "apply_points", "points": points})
        with self.assertRaisesRegex(BotError, "только торговец"):
            self.mgr.command({"action": "probe_point", "name": "search"})


class ProbeTest(tb.Base):
    def setUp(self):
        super().setUp()
        self.window("Trader", zone="CITYA", pos=(0.0, 0.0))
        self.bot.calibrate()
        self.game.join(1, "Trader", "CITYA", (0.0, 0.0))
        self.game.npcs = {}
        orig_click = self.game.click

        def click(pid, fx, fy, button):
            tx, ty = self.game.screen_to_world(pid, fx, fy)
            for (nx, ny), op in self.game.npcs.items():
                if abs(nx - tx) < 1.5 and abs(ny - ty) < 1.5:
                    self.game.request(pid, op, {})      # открылось окно торговца
                    return
            orig_click(pid, fx, fy, button)
        self.game.click = click
        # Игра сама шлёт фоновые запросы — они не должны сбить поиск.
        self.clock.on_sleep.append(lambda: self.game.request(1, 77, {}))

    def test_finds_market_npc(self):
        self.game.npcs[(3.0, 2.5)] = DEFAULT_OPCODES["auction_get_offers"]
        self.bot.probe_name = "market_npc"
        self.bot.run("probe")
        self.assertEqual(self.bot.status, "готово", self.texts()[-2:])
        pt = self.mgr.config["points"]["market_npc"]
        tx, ty = self.game.screen_to_world(1, *pt)
        self.assertLess(abs(tx - 3.0) + abs(ty - 2.5), 3.0)
        self.assertIn("auction_get_offers", self.texts()[-1])
        self.assertIn((1, "esc"), self.desk.keys)
        self.assertEqual(self.mgr.config["history"], [])               # поиск — не запуск задачи

    def test_nothing_found(self):
        self.bot.probe_name = "stash_open"
        self.bot.run("probe")
        self.assertEqual(self.bot.status, "ошибка")
        self.assertIn("не нашёл", self.texts()[-1])
        self.mgr.command({"action": "probe_point", "name": "stash_open"})
        self.bot.thread.join(30)


if __name__ == "__main__":
    unittest.main()
