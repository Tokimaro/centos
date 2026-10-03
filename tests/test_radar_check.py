"""Тесты проверки радара (check-radar): окружение, окно, сеть, файлы, живая программа,
окно радара в Windows (через подмену функций Windows)."""

import json
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock

from albion_trader import radar_check, window
from albion_trader.__main__ import main
from albion_trader.selfcheck import Report
from albion_trader.server import App, AppConfig, make_handler

try:
    from . import photon_builder as pb
except ImportError:
    import photon_builder as pb


def text(r: Report) -> str:
    return "\n".join(r.lines)


class OfflineChecksTest(unittest.TestCase):
    def test_environment_sockets_ok_and_failing(self):
        r = Report()
        sock = mock.Mock()
        with mock.patch.object(radar_check, "is_admin", return_value=True):
            radar_check.check_environment(r, open_sockets=lambda: [sock])
        sock.close.assert_called_once()
        self.assertIn("Сокеты захвата открываются (1 шт.", text(r))
        r = Report()
        with mock.patch.object(radar_check, "is_admin", return_value=False):
            radar_check.check_environment(r, open_sockets=lambda: (_ for _ in ()).throw(PermissionError("нет прав")))
        t = text(r)
        self.assertIn("Нет прав администратора", t)
        self.assertIn("Сокеты захвата не открылись: нет прав", t)
        r = Report()
        with mock.patch.object(radar_check, "is_admin", return_value=None):
            radar_check.check_environment(r, open_sockets=lambda: [])
        self.assertIn("Не удалось узнать", text(r))

    def test_is_admin_returns_bool_or_none(self):
        self.assertIn(radar_check.is_admin(), (True, False, None))

    def test_window_support_branches(self):
        r = Report()
        with mock.patch.object(window, "find_browser", return_value="/x/msedge.exe"), \
                mock.patch.object(window, "IS_WINDOWS", False):
            radar_check.check_window_support(r)
        self.assertIn("Браузер для окна найден: msedge.exe", text(r))
        self.assertIn("только в Windows", text(r))
        r = Report()
        fake_ctypes = mock.Mock()
        fake_ctypes.windll.user32 = mock.Mock(spec=["SetWindowPos", "GetWindowLongW", "SetWindowLongW",
                                                    "SetLayeredWindowAttributes", "EnumWindows", "GetWindowTextW"])
        with mock.patch.object(window, "find_browser", return_value=None), \
                mock.patch.object(window, "IS_WINDOWS", True), mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            radar_check.check_window_support(r)
        self.assertIn("не найден — окно радара откроется вкладкой", text(r))
        self.assertIn("Функции Windows для «поверх игры» и оверлея доступны", text(r))
        r = Report()
        fake_ctypes.windll.user32 = mock.Mock(spec=["SetWindowPos"])
        with mock.patch.object(window, "IS_WINDOWS", True), mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            radar_check.check_window_support(r)
        self.assertIn("Нет функций Windows: GetWindowLongW", text(r))

    def test_network(self):
        r = Report()
        radar_check.check_network(r, probe=lambda url: (True, "HTTP 200") if "github" in url else (False, "URLError"))
        t = text(r)
        self.assertIn("[ OK ] ao-bin-dumps доступен", t)
        self.assertIn("[info] render.albiononline.com недоступен", t)
        self.assertEqual(r.counts["FAIL"], 0)

    def test_reachable_handles_errors(self):
        self.assertFalse(radar_check._reachable("http://127.0.0.1:1/x", timeout=1)[0])

    def test_files_good_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            r = Report()
            radar_check.check_files(r, d)
            self.assertEqual(r.counts["FAIL"], 0)
            zm = d / "zonemaps"
            zm.mkdir()
            (zm / "index.json").write_text(json.dumps({str(i): {} for i in range(600)}))
            (zm / "0201.json").write_text(json.dumps({"tiles": [1]}))
            (zm / "bad.json").write_text("{x")
            (d / "mobs.json").write_text(json.dumps([[1]] * 1500))
            (d / "opcodes.json").write_text(json.dumps({"events": {"new_mob": 1}}))
            (d / "radar.json").write_text("{bad")
            (d / "maps").mkdir()
            (d / "maps" / "0201.png").write_bytes(b"x")
            (d / "rec.pcap").write_bytes(b"x")
            r = Report()
            radar_check.check_files(r, d)
            t = text(r)
            for s in ("Список зон: 600", "Скачанных схем зон: 2", "Повреждённые схемы", "bad.json",
                      "Справочник мобов на месте", "своих номеров событий: 1", "radar.json повреждён",
                      "Своих картинок карт: 1 (0201.png)", "Записей для перемотки (.pcap): 1"):
                self.assertIn(s, t)
            (zm / "index.json").write_text("{x")
            (d / "mobs.json").write_text("[]")
            r = Report()
            radar_check.check_files(r, d)
            self.assertIn("Список зон повреждён", text(r))
            self.assertIn("Справочник мобов неполный", text(r))


class LiveChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def feed_game(self):
        st = self.app.albion
        p = __import__("albion_trader.capture.photon", fromlist=["PhotonParser"]).PhotonParser(
            st.on_request, st.on_response, st.on_event)
        p.receive_packet(pb.packet(pb.response(2, {0: 1, 2: "Me", 8: "0201", 9: [0.0, 0.0]})))
        p.receive_packet(pb.packet(pb.event(st.ev["new_character"], {0: 5, 1: "Bob", 12: [1.0, 1.0]})))
        for i in range(6):
            p.receive_packet(pb.packet(pb.event(777, {0: 100 + i, 1: 5, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: [1.0, 2.0]})))

    def test_program_not_running(self):
        r = Report()
        self.assertFalse(radar_check.check_live(r, "http://127.0.0.1:1"))
        self.assertIn("Программа не отвечает", text(r))

    def test_no_game_events(self):
        r = Report()
        self.assertTrue(radar_check.check_live(r, self.url))
        t = text(r)
        self.assertIn("Событий от игры нет", t)
        self.assertIn("Зона не определена", t)
        self.assertIn("На радаре: пусто", t)

    def test_game_running_with_zone_and_suggestions(self):
        self.feed_game()
        self.app.zonemaps.fetch = lambda p: (_ for _ in ()).throw(OSError("нет сети"))
        r = Report()
        radar_check.check_live(r, self.url)
        t = text(r)
        self.assertIn("От игры пришли события", t)
        self.assertIn("Зона: 0201", t)
        self.assertIn("Фон зоны не готов", t)
        self.assertIn("«new_mob» на этом сервере, похоже, 777", t)
        self.assertIn("player 1", t)
        self.assertIn("История встреч: 1 игроков", t)
        self.assertIn("Запросов игры не видно", t)
        st = self.app.albion
        for i in range(9):
            st.on_request(1, {253: 82, 1: [float(i), 2.0]})
        r = Report()
        radar_check.check_live(r, self.url)
        t = text(r)
        self.assertIn("Запросы игры (исходящий трафик) видны: 9", t)
        self.assertIn("Запрос движения: код 82 (опознан автоматически)", t)
        self.assertIn("запрос 82 (move): 9 раз, с позицией 9 — 1:xy", t)

    def test_window_live_on_windows(self):
        rw = self.app.radar_window
        rw.finder = lambda: "msedge.exe"
        rw.popen = mock.Mock(return_value=mock.Mock(poll=lambda: None))
        rw.elevated, rw.shown = (lambda: False), (lambda title: True)
        with mock.patch.object(window, "IS_WINDOWS", True), \
                mock.patch.object(window, "_set_topmost", return_value=True), \
                mock.patch.object(window, "_set_overlay", return_value=True) as ov:
            r = Report()
            radar_check.check_window_live(r, self.url, pause=0)
        t = text(r)
        self.assertIn("Окно радара открыто отдельным окном", t)
        self.assertIn("«Поверх игры» применилось", t)
        self.assertIn("Оверлей включился", t)
        self.assertIn("Оверлей выключен", t)
        self.assertEqual([c.args[1:] for c in ov.call_args_list], [(True, 70), (False, 70)])
        self.assertEqual(r.counts["FAIL"], 0)

    def test_window_live_failures_and_not_windows(self):
        rw = self.app.radar_window
        rw.finder = lambda: None
        rw.opener = mock.Mock()
        with mock.patch.object(window, "IS_WINDOWS", False):
            r = Report()
            radar_check.check_window_live(r, self.url, pause=0)
        self.assertIn("вкладкой браузера", text(r))
        self.assertIn("только в Windows", text(r))
        with mock.patch.object(window, "IS_WINDOWS", True), \
                mock.patch.object(window, "_set_topmost", return_value=False), \
                mock.patch.object(window, "_set_overlay", return_value=False):
            r = Report()
            radar_check.check_window_live(r, self.url, pause=0)
        self.assertEqual(r.counts["FAIL"], 3)
        rw.elevated = lambda: True
        with mock.patch.object(window, "IS_WINDOWS", False):
            r = Report()
            radar_check.check_window_live(r, self.url, pause=0)
        self.assertIn("Окно-приложение не открылось (программа запущена от администратора)", text(r))
        r = Report()
        radar_check.check_window_live(r, "http://127.0.0.1:1", pause=0)
        self.assertIn("Программа не отвечает", text(r))

    def test_run_and_cli(self):
        self.feed_game()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(radar_check, "check_environment"), \
                mock.patch.object(radar_check, "check_network"):
            txt, out = radar_check.run(d, self.url)
            self.assertTrue(out.exists())
            self.assertIn("проверка радара", txt)
            self.assertIn("/radar-selftest.html", txt)
            with mock.patch("webbrowser.open") as wb, mock.patch("builtins.print"):
                code = main(["--data-dir", d, "check-radar", "--url", self.url])
            wb.assert_called_once_with(self.url + "/radar-selftest.html")
            self.assertEqual(code, 0)
            with mock.patch("webbrowser.open") as wb, mock.patch("builtins.print"):
                main(["--data-dir", d, "check-radar", "--url", "http://127.0.0.1:1", "--no-browser"])
            wb.assert_not_called()


class SelfcheckIncludesRadarTest(unittest.TestCase):
    def test_check_command_lists_radar_files(self):
        from albion_trader.selfcheck import _radar_files
        with tempfile.TemporaryDirectory() as d:
            r = Report()
            _radar_files(r, Path(d))
        self.assertIn("check-radar", text(r))


if __name__ == "__main__":
    unittest.main()
