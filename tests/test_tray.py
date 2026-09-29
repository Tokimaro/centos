import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from albion_trader import tray


class AutostartTest(unittest.TestCase):
    def test_commands(self):
        on = tray.autostart_commands(True)
        self.assertEqual(on[:4], ["schtasks", "/Create", "/TN", "AlbionTrader"])
        self.assertIn("HIGHEST", on)
        self.assertIn("ONLOGON", on)
        self.assertEqual(tray.autostart_commands(False), ["schtasks", "/Delete", "/TN", "AlbionTrader", "/F"])

    def test_launch_command_frozen_and_source(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Apps\AlbionTrader.exe"):
            self.assertEqual(tray.launch_command(), r'"C:\Apps\AlbionTrader.exe" --autostart')
        with mock.patch.object(sys, "executable", r"C:\Python312\python.exe"):
            cmd = tray.launch_command()
            self.assertIn("pythonw.exe", cmd)
            self.assertIn("-m albion_trader", cmd)
            self.assertIn("serve --tray", cmd)

    def test_query_and_set_on_windows(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(tray, "IS_WINDOWS", True):
            self.assertTrue(tray.autostart_enabled(run))
            self.assertTrue(tray.set_autostart(True, run))
            self.assertEqual(calls[-1][1], "/Create")
            failing = lambda cmd, **kw: mock.Mock(returncode=1, stdout="", stderr="Отказано в доступе")
            with self.assertRaises(OSError):
                tray.set_autostart(True, failing)
            self.assertFalse(tray.set_autostart(False, failing))   # удаление несуществующей задачи — не ошибка

    def test_non_windows_is_noop(self):
        with mock.patch.object(tray, "IS_WINDOWS", False):
            self.assertFalse(tray.autostart_enabled())
            with self.assertRaises(OSError):
                tray.set_autostart(True)
            t = tray.Tray("http://x", on_exit=lambda: None)
            self.assertFalse(t.start())
            t.notify("a", "b")   # не падает


class LauncherTest(unittest.TestCase):
    def setUp(self):
        import launcher
        self.launcher = launcher
        self.env = mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, clear=False)
        self.env.start()
        os_env = __import__("os").environ
        self.saved = os_env.pop("ALBION_TRADER_DATA", None)

    def tearDown(self):
        self.env.stop()
        if self.saved is not None:
            __import__("os").environ["ALBION_TRADER_DATA"] = self.saved

    def test_default_args_parse_with_real_parser(self):
        from albion_trader.__main__ import build_parser
        for args in (self.launcher.default_args(), self.launcher.default_args(autostart=True)):
            ns = build_parser().parse_args(args)
            self.assertEqual((ns.cmd, ns.tray, ns.fetch_reference, ns.log_file), ("serve", True, True, True))
            self.assertTrue(ns.data_dir.endswith(str(Path("AlbionTrader") / "data")))
        self.assertTrue(build_parser().parse_args(self.launcher.default_args()).open_browser)
        self.assertFalse(build_parser().parse_args(self.launcher.default_args(autostart=True)).open_browser)

    def test_user_args_keep_data_dir(self):
        args = self.launcher.build_args(["serve", "--port", "9000"])
        self.assertEqual(args[:1], ["--data-dir"])
        self.assertEqual(args[2:], ["serve", "--port", "9000"])
        self.assertEqual(self.launcher.build_args(["--data-dir", "X", "serve"]), ["--data-dir", "X", "serve"])

    def test_env_data_dir_respected(self):
        with mock.patch.dict("os.environ", {"ALBION_TRADER_DATA": "D:\\AlbionData"}):
            self.assertEqual(str(self.launcher.default_data_dir()), "D:\\AlbionData")

    def test_serve_port(self):
        self.assertEqual(self.launcher.serve_port(["--data-dir", "x", "serve", "--port", "9000"]), 9000)
        self.assertEqual(self.launcher.serve_port(["--data-dir", "x", "serve"]), 8484)
        self.assertIsNone(self.launcher.serve_port(["--data-dir", "x", "update-items"]))
        self.assertEqual(self.launcher.serve_port(["serve", "--port=9001"]), 9001)

    def test_already_running_probe(self):
        import threading
        from http.server import ThreadingHTTPServer
        from albion_trader.server import App, AppConfig, make_handler
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json"))
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                self.assertTrue(self.launcher.already_running(port))
                # Интерфейс под паролем (в том числе для локальных запросов) — всё равно «запущен».
                app.config.password, app.config.auth_local = "p", True
                self.assertTrue(self.launcher.already_running(port))
            finally:
                httpd.shutdown()
                httpd.server_close()
            self.assertFalse(self.launcher.already_running(port, timeout=0.5))

    def test_second_instance_opens_browser(self):
        with mock.patch.object(self.launcher, "already_running", return_value=True), \
                mock.patch.object(self.launcher.webbrowser, "open") as opened, \
                mock.patch.object(self.launcher, "_redirect_output"):
            self.assertEqual(self.launcher.run([]), 0)
            opened.assert_called_once()
            opened.reset_mock()
            self.assertEqual(self.launcher.run(["--autostart"]), 0)
            opened.assert_not_called()

    def test_bad_args_logged_not_crash(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(self.launcher, "already_running", return_value=False):
            import io
            buf = io.StringIO()
            with mock.patch.object(sys, "stderr", buf):
                code = self.launcher.run(["--data-dir", d, "serve", "--prot", "1"])
            self.assertEqual(code, 2)
            self.assertIn("--prot", buf.getvalue())

    def test_autostart_command_uses_flag(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Apps\AlbionTrader.exe"):
            self.assertEqual(tray.launch_command(), r'"C:\Apps\AlbionTrader.exe" --autostart')


class SystemApiTest(unittest.TestCase):
    def test_system_info(self):
        from albion_trader.server import App, AppConfig
        with tempfile.TemporaryDirectory() as d:
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "i.json", capture=False))
            info = app.api_system({})
            self.assertIn("version", info)
            self.assertEqual(info["windows"], sys.platform == "win32")


class FetchReferenceTest(unittest.TestCase):
    def test_first_run_downloads_and_reloads(self):
        import json
        import time as _time
        from albion_trader import server
        from albion_trader.server import App, AppConfig

        def fake_catalog(path):
            Path(path).write_text(json.dumps({"names": {"T4_BAG": {"ru": "Сумка", "en": "Bag"}}, "index": {}}))
            return 1

        def fake_gamedata(path):
            Path(path).write_text(json.dumps({"recipes": {"T4_BAG": {"res": [], "n": 1, "kind": "craft"}},
                                              "items": {}}))
            return {}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(server, "download_catalog", fake_catalog), \
                mock.patch.object(server, "download_gamedata", fake_gamedata):
            app = App(AppConfig(db_path=Path(d) / "m.db", items_path=Path(d) / "items.json", capture=False))
            self.assertEqual(app.catalog.name("T4_BAG"), "T4_BAG")
            app.fetch_reference_async()
            deadline = _time.time() + 5
            while not app.gamedata and _time.time() < deadline:
                _time.sleep(0.02)
            self.assertEqual(app.catalog.name("T4_BAG"), "Сумка")
            self.assertTrue(app.gamedata)
