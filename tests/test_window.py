import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from albion_trader import window
from albion_trader.server import App, AppConfig, make_handler


class FakeProc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0


class WindowTest(unittest.TestCase):
    def test_find_browser_prefers_edge_then_path(self):
        env = {"ProgramFiles(x86)": r"C:\PF86", "ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        edge = str(Path(r"C:\PF86") / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        chrome = str(Path(r"C:\PF") / "Google" / "Chrome" / "Application" / "chrome.exe")
        self.assertEqual(window.find_browser(env, exists=lambda p: p in (edge, chrome), which=lambda n: None), edge)
        self.assertEqual(window.find_browser(env, exists=lambda p: p == chrome, which=lambda n: None), chrome)
        self.assertEqual(window.find_browser({}, exists=lambda p: False,
                                             which=lambda n: "/usr/bin/chromium" if n == "chromium" else None),
                         "/usr/bin/chromium")
        self.assertIsNone(window.find_browser({}, exists=lambda p: False, which=lambda n: None))

    def test_app_command(self):
        cmd = window.app_command("edge.exe", "http://127.0.0.1:8484/companion.html", "D:/prof", (500, 700))
        self.assertEqual(cmd[:4], ["edge.exe", "--app=http://127.0.0.1:8484/companion.html",
                                   "--user-data-dir=D:/prof", "--window-size=500,700"])

    def test_open_launches_once_and_falls_back(self):
        launched, opened = [], []
        proc = FakeProc()

        def popen(cmd, **kw):
            launched.append(cmd)
            return proc
        with tempfile.TemporaryDirectory() as d:
            w = window.CompanionWindow("http://127.0.0.1:9000/", Path(d) / "p", popen=popen,
                                       finder=lambda: "/usr/bin/chromium",
                                       opener=lambda url, new=0: opened.append(url))
            self.assertEqual(w.open()["mode"], "app")
            self.assertTrue(launched[0][1].endswith("127.0.0.1:9000/companion.html"))
            self.assertTrue((Path(d) / "p").is_dir())
            self.assertTrue(w.open()["already"])          # уже открыто — второй раз не запускаем
            self.assertEqual(len(launched), 1)
            proc.alive = False                            # окно закрыли — откроется снова
            w.open()
            self.assertEqual(len(launched), 2)
            nob = window.CompanionWindow("http://x", Path(d) / "q", popen=popen, finder=lambda: None,
                                         opener=lambda url, new=0: opened.append(url))
            self.assertEqual(nob.open()["mode"], "browser")
            self.assertEqual(opened, ["http://x/companion.html"])

    def test_popen_error_falls_back_to_browser(self):
        opened = []

        def popen(cmd, **kw):
            raise OSError("нет доступа")
        with tempfile.TemporaryDirectory() as d:
            w = window.CompanionWindow("http://x", d, popen=popen, finder=lambda: "edge",
                                       opener=lambda url, new=0: opened.append(url))
            self.assertEqual(w.open()["mode"], "browser")

    def test_topmost_state(self):
        with tempfile.TemporaryDirectory() as d:
            w = window.CompanionWindow("http://x", d)
            res = w.set_topmost(True)
            self.assertTrue(res["topmost"])
            if not window.IS_WINDOWS:
                self.assertFalse(res["applied"])


class WindowApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "items.json", capture=False))
        self.opened = []
        self.app.window = window.CompanionWindow("http://127.0.0.1:1", d / "p", finder=lambda: None,
                                                 opener=lambda url, new=0: self.opened.append(url))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, body):
        req = urllib.request.Request(self.base + "/api/window", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_open_and_topmost(self):
        with urllib.request.urlopen(self.base + "/api/window") as r:
            self.assertEqual(json.load(r)["topmost"], False)
        self.assertEqual(self.post({"action": "open"})["mode"], "browser")
        self.assertEqual(self.opened, ["http://127.0.0.1:1/companion.html"])
        self.assertTrue(self.post({"topmost": True})["topmost"])
        self.assertEqual(self.post({"bogus": 1})["topmost"], True)

    def test_companion_page_served(self):
        with urllib.request.urlopen(self.base + "/companion.html") as r:
            html = r.read().decode()
        self.assertIn("<title>Albion Trader — инструменты</title>", html)
        self.assertIn("js/tools.js", html)
        with urllib.request.urlopen(self.base + "/js/tools.js") as r:
            self.assertIn("openCompanion", r.read().decode())

    def test_remote_clients_cannot_launch(self):
        # Имитируем запрос с другого компьютера (сеть, с паролем).
        self.app.config.password = "p"
        handler = make_handler(self.app)
        orig = handler.setup

        def setup(h):
            orig(h)
            h.client_address = ("10.0.0.5", 5555)
        with mock.patch.object(handler, "setup", setup):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                import base64
                req = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/api/window", method="POST",
                    data=b'{"action": "open"}',
                    headers={"Content-Type": "application/json",
                             "Authorization": "Basic " + base64.b64encode(b"u:p").decode()})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req)
                self.assertEqual(cm.exception.code, 403)
                self.assertEqual(self.opened, [])
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
