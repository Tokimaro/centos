import unittest

from albion_trader.production import CraftParams, PriceBook, craft_table, return_rate
from albion_trader.gamedata import GameData

NOW = 1_800_000_000


def o(item, loc, typ, price, quality=1, amount=10):
    return {"item_id": item, "location": loc, "auction_type": typ, "price": price, "amount": amount,
            "quality": quality, "seen_at": NOW}


GD = GameData({
    "items": {"T4_2H_BOW": {"t": 4, "cat": "weapons", "cc": "bow", "v": 512},
              "T4_PLANKS": {"t": 4, "cat": "crafting", "sub": "refinedresources", "cc": "wood", "v": 16},
              "T4_ART": {"t": 4, "cat": "artefacts"},
              "T4_ART_BOW": {"t": 4, "cat": "weapons", "cc": "bow", "v": 600}},
    "recipes": {
        "T4_2H_BOW": {"res": [["T4_PLANKS", 32, True]], "n": 1, "silver": 0, "focus": 1715, "kind": "craft"},
        "T4_ART_BOW": {"res": [["T4_PLANKS", 20, True], ["T4_ART", 1, False]], "n": 1, "silver": 0,
                       "focus": 2000, "kind": "craft"},
        "T4_PLANKS": {"res": [["T4_WOOD", 2, True], ["T3_PLANKS", 1, True]], "n": 1, "silver": 0,
                      "focus": 54, "kind": "refine"},
    },
    "cities": {"lymhurst": {"craft": 0.18, "refine": 0.18, "mods": {"bow": 0.15}},
               "fort_sterling": {"craft": 0.18, "refine": 0.18, "mods": {"wood": 0.40}}},
})


class ReturnRateTest(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(return_rate(0.18), 0.1525, places=4)       # королевский город
        self.assertAlmostEqual(return_rate(0.18 + 0.59), 0.4350, places=4)  # + фокус
        self.assertAlmostEqual(return_rate(0.58), 0.3671, places=4)       # переработка в городе-специализации
        self.assertEqual(return_rate(0), 0)


class CraftTest(unittest.TestCase):
    def book(self):
        return PriceBook([o("T4_PLANKS", "lymhurst", "offer", 300), o("T4_PLANKS", "lymhurst", "request", 250),
                          o("T4_2H_BOW", "lymhurst", "request", 12000), o("T4_2H_BOW", "lymhurst", "offer", 14000),
                          o("T4_ART", "lymhurst", "offer", 5000), o("T4_ART_BOW", "lymhurst", "request", 20000),
                          o("T4_WOOD", "fort_sterling", "offer", 100), o("T3_PLANKS", "fort_sterling", "offer", 90),
                          o("T4_PLANKS", "fort_sterling", "request", 280)])

    def rows(self, **kw):
        base = dict(buy_market="lymhurst", sell_market="lymhurst", craft_city="lymhurst", tax=0.04)
        base.update(kw)
        return {r["item_id"]: r for r in craft_table(GD, self.book(), CraftParams(**base))}

    def test_craft_with_city_bonus(self):
        r = self.rows()["T4_2H_BOW"]
        rr = return_rate(0.33)
        self.assertAlmostEqual(r["return_rate"], round(rr * 100, 2))
        self.assertAlmostEqual(r["cost"], round(300 * 32 * (1 - rr), 2))
        self.assertAlmostEqual(r["revenue"], 12000 * 0.96)
        self.assertAlmostEqual(r["profit"], round(12000 * 0.96 - 300 * 32 * (1 - rr), 2))

    def test_artifact_not_returned_and_station_fee(self):
        r = self.rows(station_fee=400)["T4_ART_BOW"]
        rr = return_rate(0.33)
        fee = 600 * 0.1125 * 400 / 100
        self.assertAlmostEqual(r["cost"], round(300 * 20 * (1 - rr) + 5000 + fee, 2))
        self.assertAlmostEqual(r["station_fee"], fee)

    def test_focus_and_modes(self):
        r = self.rows(focus=True, buy_mode="order", sell_mode="order")["T4_2H_BOW"]
        rr = return_rate(0.33 + 0.59)
        self.assertAlmostEqual(r["cost"], round(251 * 1.025 * 32 * (1 - rr), 2))
        self.assertAlmostEqual(r["revenue"], round(13999 * (1 - 0.04 - 0.025), 2))

    def test_refine_mode_and_missing_prices(self):
        rows = self.rows(kind="refine", buy_market="fort_sterling", sell_market="fort_sterling",
                         craft_city="fort_sterling")
        r = rows["T4_PLANKS"]
        rr = return_rate(0.58)
        self.assertAlmostEqual(r["cost"], round((100 * 2 + 90) * (1 - rr), 2))
        self.assertNotIn("T4_2H_BOW", rows)
        # Без цены ресурса рецепт скрыт, с include_incomplete — показан без прибыли.
        self.assertNotIn("T4_PLANKS", self.rows(kind="refine"))
        inc = self.rows(kind="refine", include_incomplete=True)["T4_PLANKS"]
        self.assertIsNone(inc["profit"])
        self.assertEqual(sorted(inc["missing"]), ["T3_PLANKS", "T4_WOOD"])

    def test_category_filter(self):
        self.assertEqual(set(self.rows(category="artefacts")), set())
        self.assertEqual(set(self.rows(category="weapons")), {"T4_2H_BOW", "T4_ART_BOW"})


class EnchantTest(unittest.TestCase):
    def test_chain_costs_and_profit(self):
        from albion_trader.production import enchant_table
        gd = GameData({"items": {}, "recipes": {},
                       "upgrades": {"T4_BOW": [[1, "T4_RUNE", 10], [2, "T4_SOUL", 10], [3, "T4_RELIC", 10]]}})
        book = PriceBook([o("T4_BOW", "m", "offer", 1000), o("T4_RUNE", "m", "offer", 50),
                          o("T4_SOUL", "m", "offer", 200),
                          o("T4_BOW@1", "m", "request", 2000), o("T4_BOW@1", "m", "offer", 2500),
                          o("T4_BOW@2", "m", "request", 3000),
                          o("T4_BOW@1", "m", "offer", 900, quality=2)])
        rows = {(r["from_level"], r["to_level"], r["quality"]): r
                for r in enchant_table(gd, book, "m", "m", "instant", "instant", 0.04)}
        r01 = rows[(0, 1, 1)]
        self.assertAlmostEqual(r01["cost"], 1000 + 500)
        self.assertAlmostEqual(r01["profit"], 2000 * 0.96 - 1500)
        self.assertAlmostEqual(r01["saving"], 2500 - 1500)
        r02 = rows[(0, 2, 1)]
        self.assertAlmostEqual(r02["cost"], 1000 + 500 + 2000)
        self.assertEqual([m["item_id"] for m in r02["materials"]], ["T4_RUNE", "T4_SOUL"])
        # Цепочка до .3 обрывается: нет цены реликвии.
        self.assertNotIn((0, 3, 1), rows)
        # Качество 2: .1 куплен за 900, продать .2 качества 2 негде — строк нет.
        self.assertNotIn((1, 2, 2), rows)
