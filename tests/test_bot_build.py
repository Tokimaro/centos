"""Билд бота: умения предметов из справочника, порядок и условия, изучение перезарядок."""

import unittest

from albion_trader.bot_build import KeyLearner, auto_skills, detect_build
from albion_trader.bot_core import parse_skills
from albion_trader.gamedata import parse_spells, spell_kind
from albion_trader.radar import Radar

RAW_ITEMS = {"items": {
    "weapon": [
        {"@uniquename": "T4_2H_BOW", "@slottype": "mainhand", "craftingspelllist": {"craftspell": [
            {"@uniquename": "MULTISHOT", "@slots": "1"}, {"@uniquename": "DEADLYSHOT", "@slots": "1"},
            {"@uniquename": "SPEEDSHOT", "@slots": "2"}, {"@uniquename": "BIGARROW", "@slots": "3"},
            {"@uniquename": "PASSIVE_X"}]}},
        {"@uniquename": "T5_2H_BOW", "@slottype": "mainhand", "craftingspelllist": {"@reference": "T4_2H_BOW"}},
    ],
    "equipmentitem": [
        {"@uniquename": "T4_HEAD_PLATE", "@slottype": "head", "craftingspelllist": {"craftspell": [
            {"@uniquename": "STONESKIN"}, {"@uniquename": "PASSIVE_X"}]}},
        {"@uniquename": "T4_ARMOR_CLOTH", "@slottype": "armor", "craftingspelllist": {"craftspell": [
            {"@uniquename": "SELFHEAL"}]}},
        {"@uniquename": "T4_SHOES_LEATHER", "@slottype": "shoes", "craftingspelllist": {"craftspell": [
            {"@uniquename": "DODGE"}]}},
        {"@uniquename": "T4_CAPE", "@slottype": "cape", "craftingspelllist": {"craftspell": {"@uniquename": "P"}}},
    ],
    "consumableitem": [
        {"@uniquename": "T4_POTION_HEAL", "@slottype": "potion", "@consumespell": "POTION_HEAL"},
        {"@uniquename": "T4_MEAL_SOUP", "@slottype": "food", "@consumespell": "FOOD_SOUP"},
    ]}}


def spell(name, cd, target="enemy", cat="damage", ui="damage", cast="0"):
    return {"@uniquename": name, "@recastdelay": str(cd), "@castingtime": cast, "@target": target,
            "@category": cat, "@uitype": ui}


RAW_SPELLS = {"spells": {"passivespell": [{"@uniquename": "PASSIVE_X"}], "activespell": [
    spell("MULTISHOT", 3, "ground"), spell("DEADLYSHOT", 2, "ground"),
    spell("SPEEDSHOT", 10, "enemy", "movementbuff", "movement"),
    spell("BIGARROW", 30, "enemy", cast="1.2"),
    spell("STONESKIN", 30, "self", "buff", "buff"),
    spell("SELFHEAL", 25, "self", "instant", "heal"),
    spell("DODGE", 20, "ground", "instant", "movement"),
    spell("POTION_HEAL", 105, "self", "instant", "heal"),
    spell("FOOD_SOUP", 0, "self", "foodbuff", "heal", cast="0.8"),
]}}

BOOK = parse_spells(RAW_ITEMS, RAW_SPELLS)
EQUIP = {"оружие": "T5_2H_BOW@1", "голова": "T4_HEAD_PLATE", "броня": "T4_ARMOR_CLOTH",
         "обувь": "T4_SHOES_LEATHER", "плащ": "T4_CAPE", "зелье": "T4_POTION_HEAL", "еда": "T4_MEAL_SOUP"}


class SpellDataTest(unittest.TestCase):
    def test_parse_items_and_spells(self):
        items, spells = BOOK["items"], BOOK["spells"]
        self.assertEqual(items["T5_2H_BOW"], {"slot": "mainhand", "q": ["MULTISHOT", "DEADLYSHOT"],
                                              "w": ["SPEEDSHOT"], "e": ["BIGARROW"]})   # по ссылке, без пассивных
        self.assertEqual(items["T4_HEAD_PLATE"], {"slot": "head", "a": ["STONESKIN"]})
        self.assertEqual(items["T4_POTION_HEAL"], {"slot": "potion", "a": ["POTION_HEAL"]})
        self.assertNotIn("T4_CAPE", items)                       # плащ — только пассивное
        self.assertEqual(spells["BIGARROW"], [30.0, 1.2, "enemy", "damage"])
        self.assertEqual([spells[n][3] for n in ("SPEEDSHOT", "STONESKIN", "SELFHEAL", "DODGE")],
                         ["move", "buff", "heal", "move"])
        self.assertEqual(parse_spells(RAW_ITEMS, None), {"items": {}, "spells": {}})
        self.assertEqual(spell_kind({"@category": "crowdcontrol"}), "cc")
        self.assertEqual(spell_kind({"@category": "buff_damageshield", "@uitype": "buff", "@target": "self"}),
                         "shield")


class BuildTest(unittest.TestCase):
    def test_detect_build_keys(self):
        b = {x.key: x for x in detect_build(EQUIP, BOOK, potion_key="1", food_key="2")}
        self.assertEqual(sorted(b), ["1", "2", "d", "e", "f", "q", "r", "w"])
        self.assertEqual((b["q"].cd, b["q"].aim, b["q"].spells), (2.0, True, ("MULTISHOT", "DEADLYSHOT")))
        self.assertEqual((b["e"].cd, b["e"].cast, b["e"].kind), (30.0, 1.2, "damage"))
        self.assertEqual((b["r"].kind, b["d"].kind, b["f"].kind, b["2"].kind), ("heal", "buff", "move", "food"))
        self.assertEqual(b["2"].cd, 10.0)                  # 0 в справочнике — перезарядка по умолчанию
        self.assertEqual({x.key for x in detect_build(EQUIP, BOOK)}, {"q", "w", "e", "r", "d", "f"})
        self.assertEqual(detect_build({}, BOOK), [])
        self.assertEqual(detect_build(EQUIP, {}), [])

    def test_auto_skills_order_and_conditions(self):
        build = detect_build(EQUIP, BOOK, potion_key="1", food_key="2")
        skills = auto_skills(build)
        self.assertEqual([s.key for s in skills], ["r", "d", "e", "q"])    # рывки, зелье и еда — не в бою
        r, d, e, q = skills
        self.assertEqual((r.hp_below, r.self_cast, r.aim), (60.0, True, False))
        self.assertEqual((d.hp_below, d.self_cast), (75.0, True))
        self.assertEqual((e.cd, e.cast, e.aim), (30.0, 1.2, True))
        skills = auto_skills(build, parse_skills("q:5@boss f:20"), learned={"e": 24.0})
        self.assertEqual([s.key for s in skills], ["q", "f", "r", "d", "e"])  # свои строки важнее
        self.assertTrue(skills[0].boss)
        self.assertEqual(skills[-1].cd, 24.0)

    def test_parse_aim_condition(self):
        self.assertTrue(parse_skills("q:3@aim")[0].aim)


class LearnerTest(unittest.TestCase):
    def simulate(self, real, prior, start=0.0, until=400.0):
        lr = KeyLearner(prior)
        t, last = start, -1e9
        while t < until:
            if t >= lr.ready_at:
                if t - last >= real:
                    last = t
                    lr.ok(t)
                else:
                    lr.fail(t)
            t += 0.6 if t < lr.ready_at else 0.35
        return lr

    def test_converges_from_both_sides(self):
        for real, prior in ((8.0, 2.0), (8.0, 20.0), (3.0, 3.0), (15.0, 10.0)):
            lr = self.simulate(real, prior)
            self.assertIsNotNone(lr.learned, (real, prior))
            self.assertLessEqual(abs(lr.learned - real), 1.0, (real, prior, lr.state()))

    def test_empty_key_rests_and_restore(self):
        lr = KeyLearner(5)
        for i in range(4):
            lr.fail(i * 3.0)
        self.assertGreaterEqual(lr.ready_at, 9.0 + 60)       # клавиша пустая — минуту не трогаем
        lr = KeyLearner(5, cd=7.0, lo=6.8)
        self.assertEqual(lr.learned, 7.0)                    # сохранённое значение
        lr.ok(100.0)
        self.assertEqual(lr.ready_at, 107.0)
        lr.ok(103.0)                                          # сработало раньше — перезарядка короче
        self.assertEqual(lr.hi, 3.0)
        self.assertLess(lr.lo, 3.0)
        self.assertEqual(lr.state()["cd"], 3.0)


class OwnEquipmentTest(unittest.TestCase):
    def test_equipment_event_for_self(self):
        r = Radar(item_of=lambda i: {1: "T4_2H_BOW", 3: "T4_HEAD_PLATE"}.get(i))
        r.on_join({0: 7, 2: "Me", 9: [0.0, 0.0]})
        r.on_event("character_equipment_changed", {0: 8, 2: [3, 0, 3]})   # чужой
        self.assertEqual(r.my_equipment(), {})
        r.on_event("character_equipment_changed", {0: 7, 2: [1, 0, 3, 0, 0, 0, 0, 0, 0, 0]})
        self.assertEqual(r.my_equipment(), {"оружие": "T4_2H_BOW", "голова": "T4_HEAD_PLATE"})
        r.on_join({0: 7, 2: "Me", 9: [5.0, 5.0]})                    # смена зоны — экипировка остаётся
        self.assertEqual(len(r.my_equipment()), 2)


if __name__ == "__main__":
    unittest.main()
