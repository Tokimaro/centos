"""Регрессии, найденные на Windows (GitHub Actions, windows-latest)."""

import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock

from albion_trader import selfcheck
from albion_trader.server import App, AppConfig, make_handler


class SelfcheckClosesDatabaseTest(unittest.TestCase):
    """`with sqlite3.connect()` не закрывает файл — на Windows временная папка не удалялась
    (PermissionError WinError 32 в трёх тестах самопроверки)."""

    def test_every_connection_closed(self):
        opened = []
        real = sqlite3.connect

        def tracking(*a, **kw):
            c = real(*a, **kw)
            opened.append(c)
            return c
        with tempfile.TemporaryDirectory() as d:
            App(AppConfig(db_path=Path(d) / "market.db", items_path=Path(d) / "items.json", capture=False))
            with mock.patch.object(selfcheck.sqlite3, "connect", tracking):
                selfcheck.run(d, url="http://127.0.0.1:9")
        self.assertTrue(opened)
        for c in opened:
            with self.assertRaises(sqlite3.ProgrammingError):    # закрыто
                c.execute("SELECT 1")


class PostRejectedWithBodyTest(unittest.TestCase):
    """Ответ на POST до чтения тела обрывал соединение в Windows (WinError 10053)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.app = App(AppConfig(db_path=d / "m.db", items_path=d / "i.json", capture=False, token="secret"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, path, body: bytes, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_rejections_are_delivered(self):
        big = json.dumps({"Orders": [{"x": "y" * 100}] * 3000}).encode()   # ~0,3 МБ
        self.assertEqual(self.post("/marketorders.ingest", big)[0], 403)          # без токена
        self.assertEqual(self.post("/secret/unknown.topic", big)[0], 404)
        self.assertEqual(self.post("/api/settings", big, ctype="text/plain")[0], 415)
        status, body = self.post("/secret/marketorders.ingest", json.dumps({"Orders": []}).encode())
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
