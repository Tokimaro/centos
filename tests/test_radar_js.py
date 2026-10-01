"""Тесты логики интерфейса радара (static/js/radar-selftest.js) — через Node, если он есть.

Те же тесты (плюс отрисовка на canvas) открываются в браузере: /radar-selftest.html.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

RUNNER = Path(__file__).parent / "js" / "run_radar_js.js"


@unittest.skipUnless(shutil.which("node"), "Node.js не установлен — JS-тесты радара проверяются в браузере")
class RadarJsTest(unittest.TestCase):
    def test_all_js_tests_pass(self):
        out = subprocess.run(["node", str(RUNNER)], capture_output=True, text=True, timeout=60, encoding="utf-8")
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 15)
        failed = [f"{r['name']}: {r['error']}" for r in results if not r["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(out.returncode, 0)
