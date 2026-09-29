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
            self.assertEqual(tray.launch_command(), r'"C:\Apps\AlbionTrader.exe"')
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
    def test_default_args(self):
        import launcher
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}):
            args = launcher.default_args()
        self.assertEqual(args[0], "--data-dir")
        self.assertTrue(args[1].endswith(str(Path("AlbionTrader") / "data")))
        for flag in ("serve", "--tray", "--open-browser", "--fetch-reference", "--log-file"):
            self.assertIn(flag, args)


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
