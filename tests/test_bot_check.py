"""Тесты проверки ботов (check-bots) на подменённых функциях Windows."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from albion_trader import bot_check, radar_check
from albion_trader.__main__ import main
from albion_trader.bot_win import Desktop, WinApi
from albion_trader.selfcheck import Report

try:
    from .test_bot_win import desktop
except ImportError:
    from test_bot_win import desktop


def text(r):
    return "\n".join(r.lines)


SNAP = {"enabled": True, "macros": {"open": "click 0.5 0.4", "order": "click @search\nexpect 2000",
                                    "broken": "click 9 9"},
        "game": {"character": "Alice", "traffic": True, "zone": "0201", "zone_name": "Thetford", "size": "1600x900",
                 "calibrated": True, "windows": 2},
        "bot": {"status": "ошибка", "log": [{"text": "ошибка: окно игры закрыто"}]},
        "places": [{"name": "банк", "zone": "0000", "zone_name": "Thetford"}],
        "points": [{"name": "search", "label": "Поле поиска", "value": [0.3, 0.2]},
                   {"name": "price", "label": "Поле цены", "value": None}],
        "points_size": "1280x720"}


class BotCheckTest(unittest.TestCase):
    def test_support_and_windows(self):
        d, u, *_ = desktop()
        r = Report()
        with mock.patch.object(radar_check, "is_admin", return_value=False):
            self.assertTrue(bot_check.check_support(r, d))
        wins = bot_check.check_windows(r, d)
        t = text(r)
        self.assertIn("Функции Windows для кликов", t)
        self.assertIn("Нет прав администратора", t)
        self.assertIn("Найдено окон игры: 2", t)
        self.assertIn("UDP-порты: 50001, 50002", t)
        self.assertEqual(len(wins), 2)

    def test_missing_functions_and_no_windows(self):
        d, u, k, i = desktop(rows=[])
        r = Report()
        d.api = WinApi(object(), k, object())
        with mock.patch.object(radar_check, "is_admin", return_value=None):
            self.assertFalse(bot_check.check_support(r, d))
        self.assertIn("Нет функций Windows: EnumWindows", text(r))
        self.assertIn("GetExtendedUdpTable", text(r))
        self.assertIn("Не удалось узнать", text(r))
        d2, u2, *_ = desktop(rows=[])
        u2.wins = {}
        r = Report()
        self.assertEqual(bot_check.check_windows(r, d2), [])
        self.assertIn("Окна игры не найдены", text(r))

    def test_window_without_ports_small_and_mixed_sizes(self):
        d, u, *_ = desktop(rows=[(60000, 400)])
        u.GetClientRect = lambda h, ref: (setattr(ref._obj, "right", 640 if h == 15 else 1600),
                                          setattr(ref._obj, "bottom", 480 if h == 15 else 900))
        r = Report()
        bot_check.check_windows(r, d)
        t = text(r)
        self.assertIn("нет UDP-портов", t)
        self.assertIn("маленькое (640×480)", t)
        self.assertIn("Окна разного размера", t)

    def test_live(self):
        r = Report()
        self.assertTrue(bot_check.check_live(r, "http://x", fetch=lambda url: SNAP))
        t = text(r)
        for s_ in ("Бот включён", "Alice: трафик идёт, зона Thetford", "Калибровка для окна 1600x900 есть",
                   "Окон игры: 2", "Последняя ошибка бота — ошибка: окно игры закрыто", "банк (Thetford)",
                   "Не указано точек интерфейса: 1 из 2", "указывались в окне 1280x720",
                   "Макрос «open»: шагов 1 (без expect", "Макрос «order»: шагов 2", "Макрос «broken» с ошибкой: строка 1"):
            self.assertIn(s_, t)
        self.assertEqual(r.counts["FAIL"], 1)
        r = Report()
        bot_check.check_live(r, "http://x", fetch=lambda url: {
            "enabled": False, "game": {"character": "", "size": "800x600", "calibrated": False},
            "points": [{"name": "a", "label": "A", "value": [0.1, 0.1]}], "macros": {}, "places": []})
        t = text(r)
        for s_ in ("Бот выключен", "Персонаж неизвестен", "Нет калибровки для окна 800x600",
                   "Все точки интерфейса указаны", "Сохранённые места: нет"):
            self.assertIn(s_, t)
        r = Report()
        bot_check.check_live(r, "http://x", fetch=lambda url: {"enabled": True, "game": None})
        self.assertIn("Окно игры не найдено", text(r))
        r = Report()
        self.assertFalse(bot_check.check_live(r, "http://127.0.0.1:1"))
        self.assertIn("Программа не отвечает", text(r))

    def test_focus(self):
        d, u, *_ = desktop()
        wins = d.game_windows()
        r = Report()
        bot_check.check_focus(r, d, wins)
        self.assertIn("Окно игры 100 стало активным", text(r))
        self.assertIn("Прежнее окно снова активно", text(r))
        self.assertEqual(u.fg, 13)
        u.focus_ok = False
        r = Report()
        bot_check.check_focus(r, d, wins)
        self.assertIn("Windows не дала", text(r))
        r = Report()
        bot_check.check_focus(r, d, [])
        self.assertIn("пропущено", text(r))

    def test_run_and_cli(self):
        d, *_ = desktop()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(radar_check, "is_admin", return_value=True):
            txt, out = bot_check.run(tmp, "http://x", desktop=d, fetch=lambda url: SNAP, try_focus=True)
            self.assertTrue(out.exists())
            self.assertIn("проверка ботов", txt)
            self.assertIn("Переключение на окно игры", txt)
            with mock.patch("builtins.print"):
                code = main(["--data-dir", tmp, "check-bots", "--url", "http://127.0.0.1:1"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "bots_check.txt").exists())
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("albion_trader.bot_win.IS_WINDOWS", False):
                txt, _ = bot_check.run(tmp, "http://127.0.0.1:1", desktop=Desktop(WinApi()))
            self.assertIn("только в Windows", txt)


if __name__ == "__main__":
    unittest.main()
