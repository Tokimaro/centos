import unittest

from albion_trader.gamedata import GameData, build

RAW_ITEMS = {"items": {
    "weapon": [{
        "@uniquename": "T4_2H_BOW", "@tier": "4", "@shopcategory": "weapons", "@shopsubcategory1": "bow",
        "@craftingcategory": "bow",
        "craftingrequirements": {"@silver": "0", "@craftingfocus": "1715",
                                 "craftresource": {"@uniquename": "T4_PLANKS", "@count": "32"}},
        "enchantments": {"enchantment": [
            {"@enchantmentlevel": "1",
             "craftingrequirements": {"craftresource": {"@uniquename": "T4_PLANKS_LEVEL1", "@count": "32",
                                                         "@enchantmentlevel": "1"}},
             "upgraderequirements": {"upgraderesource": {"@uniquename": "T4_RUNE", "@count": "384"}}},
            {"@enchantmentlevel": "2",
             "upgraderequirements": {"upgraderesource": {"@uniquename": "T4_SOUL", "@count": "384"}}},
        ]},
    }, {
        "@uniquename": "T4_ARTEFACT_BOW", "@tier": "4",
        "craftingrequirements": {"craftresource": [
            {"@uniquename": "T4_PLANKS", "@count": "20"},
            {"@uniquename": "T4_ARTEFACT_X", "@count": "1", "@maxreturnamount": "0"}]},
    }],
    "simpleitem": [
        {"@uniquename": "T4_PLANKS", "@tier": "4", "@shopsubcategory1": "refinedresources",
         "@craftingcategory": "wood", "@itemvalue": "16",
         "craftingrequirements": [
             {"@amountcrafted": "1", "craftresource": [
                 {"@uniquename": "T4_WOOD", "@count": "2"}, {"@uniquename": "T1_FACTION_TOKEN_1", "@count": "1"},
                 {"@uniquename": "T3_PLANKS", "@count": "1"}]},
             {"@amountcrafted": "1", "craftresource": [
                 {"@uniquename": "T4_WOOD", "@count": "2"}, {"@uniquename": "T3_PLANKS", "@count": "1"}]}]},
        {"@uniquename": "T4_PLANKS_LEVEL1", "@tier": "4", "@enchantmentlevel": "1", "@itemvalue": "32"},
    ],
    "journalitem": [{
        "@uniquename": "T4_JOURNAL_WOOD", "@tier": "4", "@maxfame": "1800", "@baselootamount": "48",
        "lootlist": {"loot": [{"@itemname": "T4_WOOD", "@weight": "90", "@itemamount": "1"},
                              {"@itemname": "T4_WOOD_LEVEL1", "@itemenchantmentlevel": "1", "@weight": "10",
                               "@itemamount": "1"}]}}],
    "farmableitem": [
        {"@uniquename": "T5_FARM_CABBAGE_SEED", "@tier": "5", "@kind": "plant", "@activefarmbonus": "0.4",
         "craftingrequirements": {"@silver": "10000"},
         "harvest": {"@growtime": "79200", "@lootlist": "T5_CABBAGE_LOOT", "seed": {"@chance": "0.8"}}},
        {"@uniquename": "T5_FARM_OX_BABY", "@tier": "5", "@kind": "animal",
         "grownitem": {"@uniquename": "T5_FARM_OX_GROWN", "@growtime": "504000", "offspring": {"@chance": "0.78"}}},
    ],
}}
RAW_LOOT = {"LootDefinition": {"Lootlist": [
    {"@name": "T5_CABBAGE_LOOT", "Item": [{"@type": "T5_CABBAGE", "@chance": "1.0", "@amount": "3-6"}]}]}}
RAW_MODS = {"craftingmodifiers": {"craftinglocation": [
    {"@clusterid": "1000", "craftingbonus": {"@value": "0.18"}, "refiningbonus": {"@value": "0.18"},
     "craftingmodifier": [{"@name": "bow", "@value": "0.15"}, {"@name": "fiber", "@value": "0.40"}]},
    {"@clusterid": "9999", "craftingbonus": {"@value": "0.18"}}]}}


class GameDataTest(unittest.TestCase):
    def setUp(self):
        self.g = GameData(build(RAW_ITEMS, RAW_LOOT, RAW_MODS))

    def test_recipes_and_enchant_ids(self):
        self.assertEqual(self.g.recipes["T4_2H_BOW"]["res"], [["T4_PLANKS", 32, True]])
        self.assertEqual(self.g.recipes["T4_2H_BOW@1"]["res"], [["T4_PLANKS_LEVEL1@1", 32, True]])
        self.assertIn("T4_PLANKS_LEVEL1@1", self.g.items)

    def test_refine_recipe_skips_faction_token_variant(self):
        r = self.g.recipes["T4_PLANKS"]
        self.assertEqual(r["kind"], "refine")
        self.assertEqual([x[0] for x in r["res"]], ["T4_WOOD", "T3_PLANKS"])

    def test_non_returnable_resource(self):
        res = dict((x[0], x[2]) for x in self.g.recipes["T4_ARTEFACT_BOW"]["res"])
        self.assertEqual(res, {"T4_PLANKS": True, "T4_ARTEFACT_X": False})

    def test_item_value_derived_from_resources(self):
        self.assertEqual(self.g.item_value("T4_2H_BOW"), 32 * 16)
        self.assertEqual(self.g.item_value("T4_PLANKS"), 16)

    def test_upgrades(self):
        self.assertEqual(self.g.upgrades["T4_2H_BOW"], [[1, "T4_RUNE", 384], [2, "T4_SOUL", 384]])

    def test_journal(self):
        j = self.g.journals["T4_JOURNAL_WOOD"]
        self.assertEqual((j["empty"], j["full"], j["base"], j["type"]),
                         ("T4_JOURNAL_WOOD_EMPTY", "T4_JOURNAL_WOOD_FULL", 48, "WOOD"))
        self.assertEqual(j["loot"][1], ["T4_WOOD_LEVEL1@1", 10, 1])

    def test_farming(self):
        p = self.g.plants[0]
        self.assertEqual(p["yield"], [["T5_CABBAGE", 1.0, 4.5]])
        self.assertEqual((p["seed_chance"], p["focus_bonus"], p["silver"]), (0.8, 0.4, 10000))
        a = self.g.animals[0]
        self.assertEqual((a["grown"], a["offspring_chance"]), ("T5_FARM_OX_GROWN", 0.78))

    def test_city_bonus(self):
        self.assertAlmostEqual(self.g.city_bonus("lymhurst", "T4_2H_BOW", False), 0.33)
        self.assertAlmostEqual(self.g.city_bonus("lymhurst", "T4_PLANKS", True), 0.18)
        self.assertAlmostEqual(self.g.city_bonus("martlock", "T4_2H_BOW", False), 0.18)
        self.assertEqual(self.g.city_bonus("", "T4_2H_BOW", False), 0.0)
        self.assertNotIn("9999", self.g.cities)

    def test_empty(self):
        self.assertFalse(GameData())


class RecipeKindTest(unittest.TestCase):
    def test_transmute_detection(self):
        from albion_trader.gamedata import recipe_kind
        self.assertEqual(recipe_kind({"sub": "resources"}, {"res": [["T4_ORE", 1, False]], "focus": 0}), "transmute")
        self.assertEqual(recipe_kind({"sub": "fragments"}, {"res": [["T4_RUNE", 1, False]], "focus": 0}), "transmute")
        self.assertEqual(recipe_kind({"sub": "refinedresources"}, {"res": [], "focus": 54}), "refine")
        self.assertEqual(recipe_kind({"sub": "bow"}, {"res": [["T4_PLANKS", 32, True]], "focus": 1715}), "craft")
        # Старый файл без разметки: GameData размечает сам при загрузке.
        g = GameData({"items": {"T5_ORE": {"sub": "resources"}},
                      "recipes": {"T5_ORE": {"res": [["T4_ORE", 1, False]], "n": 1, "focus": 0, "kind": "craft"}}})
        self.assertEqual(g.recipes["T5_ORE"]["kind"], "transmute")


class WorldTest(unittest.TestCase):
    def test_parse_world(self):
        from albion_trader.gamedata import parse_world
        z = parse_world("3003: Caerleon          \n0007: Thetford Market\nbad line\n@ISLAND@x: Island")
        self.assertEqual(z["3003"], "Caerleon")
        self.assertEqual(z["0007"], "Thetford Market")
        self.assertEqual(len(z), 3)
        self.assertEqual(GameData(build(RAW_ITEMS, None, None, "1234: Somewhere")).zones, {"1234": "Somewhere"})


RAW_ACH = {"AO-Achievements": {
    "template": [
        {"@name": "COMBAT_BASE", "baselevels": {"@structure": "Fame;LP", "#text": "\n 100;1\n 200;1\n 300;2\n "}},
        {"@name": "CRAFT_SPEC", "baselevels": {"@structure": "Fame;LP", "#text": "10;1\n20;1"}},
        {"@name": "EMPTY", "baselevels": {"#text": ""}}],
    "templateachievement": [
        {"@id": "COMBAT_BOWS", "@usetemplate": "COMBAT_BASE", "@category": "fighting",
         "@missiontype": "killmobfame", "@famemultiplier": "1", "@itemforsprite": "T8_2H_BOW",
         "parentachievements": {"achievement": [{"@id": "COMBAT_HUNTER"}, {"@id": "COMBAT_BOWS_BOW"}]}},
        {"@id": "CRAFT_BOWS_LONG", "@usetemplate": "CRAFT_SPEC", "@category": "crafting",
         "@famemultiplier": "2", "parentachievements": {"achievement": {"@id": "CRAFT_BOWS"}}},
        {"@id": "BROKEN", "@usetemplate": "MISSING"}]}}
RAW_WORLD = {"?xml": {}, "world": {"clusters": {"cluster": [
    {"@id": "4206", "@displayname": "Tharcal Fissure", "@type": "OPENPVP_YELLOW",
     "exits": {"exit": [{"@targetid": "uuid-a@4208"}, {"@targetid": "uuid-b@DNG-KPR-02"},
                        {"@targetid": "uuid-c@4208"}]}},
    {"@id": "TNL-001", "@displayname": "Ouyos-Aoeuam", "@type": "TUNNEL_ROYAL", "exits": None},
    {"@id": "ISLAND-PLAYER-0001a", "@type": "PLAYERISLAND"}]}}}


class GameDataV2Test(unittest.TestCase):
    def setUp(self):
        items = {"items": {"weapon": [{
            "@uniquename": "T6_2H_NATURESTAFF", "@tier": "6", "@slottype": "mainhand", "@itempower": "900",
            "@twohanded": "true", "@combatspecachievement": "COMBAT_NATURESTAFFS_GREAT",
            "enchantments": {"enchantment": [{"@enchantmentlevel": "2", "@itempower": "1100"}]}}],
            "consumableitem": [{"@uniquename": "T8_POTION_GATHER", "@tier": "8", "@slottype": "potion",
                                "@dummyitempower": "1100"}]}}
        self.data = build(items, raw_achievements=RAW_ACH, raw_world=RAW_WORLD)
        self.g = GameData(self.data)

    def test_item_slots_and_power(self):
        staff = self.g.items["T6_2H_NATURESTAFF"]
        self.assertEqual((staff["slot"], staff["ip"], staff["2h"], staff["spec"]),
                         ("mainhand", 900, 1, "COMBAT_NATURESTAFFS_GREAT"))
        self.assertEqual(self.g.items["T6_2H_NATURESTAFF@2"]["ip"], 1100)
        self.assertEqual(self.g.items["T8_POTION_GATHER"]["ip"], 1100)

    def test_destiny(self):
        d = self.g.destiny
        self.assertEqual(d["templates"]["COMBAT_BASE"], [100, 200, 300])
        self.assertNotIn("EMPTY", d["templates"])
        bows = d["nodes"]["COMBAT_BOWS"]
        self.assertEqual((bows["base"], bows["mult"], bows["item"], bows["parents"]),
                         (True, 1, "T8_2H_BOW", ["COMBAT_HUNTER", "COMBAT_BOWS_BOW"]))
        self.assertEqual(d["nodes"]["CRAFT_BOWS_LONG"]["parents"], ["CRAFT_BOWS"])
        self.assertNotIn("BROKEN", d["nodes"])

    def test_clusters(self):
        self.assertEqual(self.g.cluster("4206"), ["Tharcal Fissure", "OPENPVP_YELLOW", ["4208", "DNG-KPR-02"]])
        self.assertEqual(self.g.cluster_type("TNL-001"), "TUNNEL_ROYAL")
        self.assertIsNone(self.g.cluster("ISLAND-PLAYER-0001a"))
        self.assertEqual(self.g.cluster_name("nope"), "nope")

    def test_old_file_without_new_sections(self):
        g = GameData({"items": {}, "recipes": {}})
        self.assertEqual((g.destiny["nodes"], g.clusters), ({}, {}))


class DownloadTest(unittest.TestCase):
    def fake_urlopen(self, missing=()):
        import io
        import json as _json
        files = {"items.json": {"items": {}}, "loot.json": {}, "craftingmodifiers.json": {},
                 "achievements.json": RAW_ACH, "cluster/world.json": RAW_WORLD,
                 "spells.json": {"spells": {"activespell": [{"@uniquename": "S", "@recastdelay": "5"}]}}}

        def urlopen(url, timeout=None):
            name = url.split("/master/", 1)[1]
            if name == "formatted/world.txt":
                return io.BytesIO("4206: Tharcal Fissure\n".encode())
            if name in missing:
                raise OSError("404")
            return io.BytesIO(_json.dumps(files[name]).encode())
        return urlopen

    def test_download_all_and_optional_missing(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from albion_trader import gamedata
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gamedata.json"
            with mock.patch.object(gamedata.urllib.request, "urlopen", self.fake_urlopen()):
                res = gamedata.download(path)
            self.assertEqual((res["destiny_nodes"], res["clusters"], res["spells"]), (2, 2, 0))
            self.assertEqual(GameData.load(path).zones, {"4206": "Tharcal Fissure"})
            with mock.patch.object(gamedata.urllib.request, "urlopen",
                                   self.fake_urlopen(("achievements.json", "cluster/world.json", "spells.json"))):
                res = gamedata.download(path)
            self.assertEqual((res["destiny_nodes"], res["clusters"]), (0, 0))
            with mock.patch.object(gamedata.urllib.request, "urlopen", self.fake_urlopen(("items.json",))):
                with self.assertRaises(OSError):
                    gamedata.download(path)


class DownloadSpellsTest(unittest.TestCase):
    def test_download_spells_merges_into_gamedata(self):
        import io
        import json as _json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from albion_trader import gamedata
        files = {"items.json": {"items": {"weapon": [{"@uniquename": "T4_X", "@slottype": "mainhand",
                                                     "craftingspelllist": {"craftspell": [
                                                         {"@uniquename": "HIT", "@slots": "1"}]}}]}},
                 "spells.json": {"spells": {"activespell": [{"@uniquename": "HIT", "@recastdelay": "4"}]}}}

        def urlopen(url, timeout=None):
            return io.BytesIO(_json.dumps(files[url.split("/master/", 1)[1]]).encode())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gamedata.json"
            path.write_text(_json.dumps({"version": 3, "recipes": {"A": {}}}))
            with mock.patch.object(gamedata.urllib.request, "urlopen", urlopen):
                self.assertEqual(gamedata.download_spells(path), 1)
            g = GameData.load(path)
            self.assertEqual(g.spells["items"]["T4_X"]["q"], ["HIT"])
            self.assertIn("A", g.recipes)                          # остальное не тронуто
            files["spells.json"] = {"spells": {}}
            with mock.patch.object(gamedata.urllib.request, "urlopen", urlopen):
                with self.assertRaises(ValueError):
                    gamedata.download_spells(path)
