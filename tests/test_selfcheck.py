import json
import tempfile
import unittest
from pathlib import Path

from albion_trader import selfcheck
from albion_trader.__main__ import main
from albion_trader.server import App, AppConfig


class SelfCheckTest(unittest.TestCase):
    def test_report_on_real_like_data(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "items.json").write_text(json.dumps({"names": {"T4_BAG": {"ru": "Сумка", "en": "Bag"}}, "index": {}}))
            (d / "gamedata.json").write_text(json.dumps({"version": 2, "items": {
                "T4_BAG": {"t": 4, "slot": "bag", "ip": 700}, "T4_LEATHER": {"t": 4}},
                "recipes": {"T4_BAG": {"res": [["T4_LEATHER", 8, True]], "n": 1, "kind": "craft"}}}))
            app = App(AppConfig(db_path=d / "market.db", items_path=d / "items.json", capture=False))
            app.ingest("marketorders.ingest", {"Orders": [
                {"Id": i, "ItemTypeId": item, "LocationId": "3008", "QualityLevel": 1, "EnchantmentLevel": 0,
                 "UnitPriceSilver": price * 10000, "Amount": 1, "AuctionType": "offer", "Expires": "2099-01-01T00:00:00"}
                for i, (item, price) in enumerate([("T4_BAG", 9000), ("T4_LEATHER", 300)], 1)]})
            app.activity.record("fame", value=100.0, amount=1.0, data={"src": "combat", "base": 100.0})
            app.albion.character_name = "СекретныйГерой"
            app.activity.record("loot", actor="СекретныйГерой", target="Mob", amount=5, data={"silver": True})
            before = (d / "market.db").read_bytes()
            text, out = selfcheck.run(d, url="http://127.0.0.1:9")      # программа не запущена
            self.assertEqual(out, d / "check_report.txt")
            self.assertTrue(out.exists())
            for section in ("Среда", "Сырые события", "«Сейчас»", "«Цепочка»", "«Билд»", "«Мета»", "«Доска»",
                            "«Авалон»", "«Данжи»", "«Учёт»"):
                self.assertIn(section, text)
            self.assertNotIn("упала", text, text)
            # Крошечный тестовый справочник честно помечается FAIL — но только в разделе справочников.
            after_env = text.split("2. Работающая программа", 1)[1]
            self.assertNotIn("[FAIL]", after_env, after_env)
            self.assertIn("[FAIL] Рецептов мало", text)
            self.assertIn("[skip] Программа не отвечает", text)
            self.assertNotIn("СекретныйГерой", text)                    # имена в отчёт не попадают
            self.assertEqual((d / "market.db").read_bytes(), before)    # данные не менялись

    def test_cli_command(self):
        with tempfile.TemporaryDirectory() as d:
            code = main(["--data-dir", d, "check", "--url", "http://127.0.0.1:9", "--out", str(Path(d) / "r.txt")])
            self.assertEqual(code, 0)
            self.assertIn("самопроверка", (Path(d) / "r.txt").read_text(encoding="utf-8"))

    def test_broken_check_reported_not_crashing(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(selfcheck, "check_build", side_effect=RuntimeError("бум")):
            text, _ = selfcheck.run(d, url="http://127.0.0.1:9")
            self.assertIn("Проверка «build» упала", text)
            self.assertIn("RuntimeError: бум", text)
            self.assertIn("«Учёт»", text)                               # остальные проверки выполнились


if __name__ == "__main__":
    unittest.main()
