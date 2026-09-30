"""Игровые справочники для калькуляторов производства.

Строятся из открытых дампов ao-data/ao-bin-dumps (``items.json``,
``loot.json``, ``craftingmodifiers.json``, ``achievements.json`` — Доска
судьбы, ``cluster/world.json`` — карта мира) командой ``update-items`` и
сохраняются компактно в ``data/gamedata.json``.

Идентификаторы везде — рыночные: для зачарованных вариантов добавляется
``@N`` (``T4_2H_BOW@1``, ``T4_PLANKS_LEVEL1@1``).
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

DUMPS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/"
GAMEDATA_VERSION = 2

# Кластеры из craftingmodifiers.json -> ключи рынков.
CLUSTER_TO_MARKET = {
    "0000": "thetford",
    "1000": "lymhurst",
    "2000": "bridgewatch",
    "3004": "martlock",
    "4000": "fort_sterling",
    "3003": "caerleon",
    "5000": "brecilien",
}


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def market_id(uid: str, level) -> str:
    level = int(_num(level))
    return f"{uid}@{level}" if level > 0 else uid


def _pick_requirement(reqs: list) -> dict | None:
    """Основной вариант рецепта: без фракционных жетонов, если такой есть."""
    reqs = [r for r in reqs if isinstance(r, dict) and r.get("craftresource")]
    for r in reqs:
        if not any("TOKEN" in x.get("@uniquename", "") for x in _as_list(r.get("craftresource"))):
            return r
    return reqs[0] if reqs else None


def _resources(req: dict) -> list:
    out = []
    for x in _as_list(req.get("craftresource")):
        uid = x.get("@uniquename")
        if not uid:
            continue
        out.append([market_id(uid, x.get("@enchantmentlevel", 0)), int(_num(x.get("@count"), 1)),
                    x.get("@maxreturnamount") != "0"])
    return out


def recipe_kind(info: dict, rec: dict) -> str:
    """craft — крафт, refine — переработка, transmute — трансмутация ресурсов/фрагментов
    (ресурс ниже тиром или руна + серебро, без фокуса и без возврата)."""
    if info.get("sub") == "refinedresources":
        return "refine"
    if info.get("sub") in ("resources", "fragments") and not rec.get("focus") \
            and all(not r[2] for r in rec.get("res", [])):
        return "transmute"
    return "craft"


def parse_world(text: str) -> dict:
    """Названия зон из formatted/world.txt: строки вида «3003: Caerleon»."""
    zones = {}
    for line in (text or "").splitlines():
        key, sep, name = line.partition(":")
        if sep and key.strip() and name.strip():
            zones[key.strip()] = name.strip()
    return zones


def parse_destiny(raw: dict | None) -> dict:
    """Доска судьбы из achievements.json: шаблоны (слава на каждый уровень) и узлы.

    В данных игры узлы доски — «достижения» (``templateachievement``): у каждого
    свой шаблон таблицы славы и множитель славы, родительские узлы и предмет,
    по которому узел изображается на доске.
    """
    if not raw:
        return {"templates": {}, "nodes": {}}
    root = next((v for k, v in raw.items() if not k.startswith("?") and isinstance(v, dict)), raw)
    templates = {}
    for t in _as_list(root.get("template")):
        name = t.get("@name")
        levels = (t.get("baselevels") or {}).get("#text") or ""
        fame = []
        for row in levels.split():
            first = row.split(";", 1)[0]
            if first:
                fame.append(_num(first))
        if name and fame:
            templates[name] = fame
    nodes = {}
    for a in _as_list(root.get("templateachievement")):
        nid, tpl = a.get("@id"), a.get("@usetemplate")
        if not nid or tpl not in templates:
            continue
        parents = [p.get("@id") for p in _as_list((a.get("parentachievements") or {}).get("achievement"))
                   if isinstance(p, dict) and p.get("@id")]
        nodes[nid] = {"tpl": tpl, "cat": a.get("@category", ""), "mission": a.get("@missiontype", ""),
                      "mult": _num(a.get("@famemultiplier"), 1.0) or 1.0, "parents": parents,
                      "item": a.get("@itemforsprite", ""), "base": tpl.endswith("_BASE")}
    return {"templates": templates, "nodes": nodes}


# Типы кластеров, которые не нужны ни маршрутам, ни журналу данжей.
_SKIP_CLUSTER_TYPES = ("DEBUG", "PLAYERISLAND", "GUILDISLAND", "SHOWROOMISLAND", "ARENA", "HIDEOUT", "TUTORIAL")


def parse_clusters(raw: dict | None) -> dict:
    """Кластеры из cluster/world.json: id → [название, тип, [id соседей]]."""
    if not raw:
        return {}
    root = next((v for k, v in raw.items() if not k.startswith("?") and isinstance(v, dict)), raw)
    out = {}
    for c in _as_list((root.get("clusters") or {}).get("cluster")):
        cid, ctype = c.get("@id"), c.get("@type", "")
        if not cid or ctype.startswith(_SKIP_CLUSTER_TYPES):
            continue
        exits = []
        for e in _as_list((c.get("exits") or {}).get("exit")):
            target = (e.get("@targetid") or "").rsplit("@", 1)[-1] if isinstance(e, dict) else ""
            if target and target != cid and target not in exits:
                exits.append(target)
        out[cid] = [c.get("@displayname") or cid, ctype, exits]
    return out


def build(raw_items: dict, raw_loot: dict | None = None, raw_modifiers: dict | None = None,
          world_text: str | None = None, raw_achievements: dict | None = None,
          raw_world: dict | None = None) -> dict:
    items_root = raw_items.get("items", raw_items)
    meta: dict[str, dict] = {}
    recipes: dict[str, dict] = {}
    upgrades: dict[str, list] = {}
    journals: dict[str, dict] = {}
    plants: list[dict] = []
    animals: list[dict] = []

    for category, entries in items_root.items():
        if not isinstance(entries, list):
            continue
        for it in entries:
            uid = it.get("@uniquename")
            if not uid:
                continue
            level = it.get("@enchantmentlevel", 0)
            mid = market_id(uid, level)
            info = {
                "t": int(_num(it.get("@tier"))),
                "cat": it.get("@shopcategory", ""),
                "sub": it.get("@shopsubcategory1", ""),
                "cc": it.get("@craftingcategory", ""),
            }
            if it.get("@itemvalue") is not None:
                info["v"] = _num(it.get("@itemvalue"))
            if it.get("@weight") is not None:
                info["w"] = _num(it.get("@weight"))
            if it.get("@slottype"):
                info["slot"] = it["@slottype"]
            ip = it.get("@itempower") or it.get("@dummyitempower")
            if ip is not None:
                info["ip"] = _num(ip)
            if it.get("@twohanded") == "true":
                info["2h"] = 1
            if it.get("@combatspecachievement"):
                info["spec"] = it["@combatspecachievement"]
            meta[mid] = info

            req = _pick_requirement(_as_list(it.get("craftingrequirements")))
            if req and category not in ("farmableitem", "journalitem"):
                recipes[mid] = {
                    "res": _resources(req),
                    "n": int(_num(req.get("@amountcrafted"), 1)) or 1,
                    "silver": _num(req.get("@silver")),
                    "focus": _num(req.get("@craftingfocus")),
                }
                recipes[mid]["kind"] = recipe_kind(info, recipes[mid])

            steps = []
            for ench in _as_list((it.get("enchantments") or {}).get("enchantment")):
                lvl = int(_num(ench.get("@enchantmentlevel")))
                emid = market_id(uid, lvl)
                meta[emid] = dict(info)
                if ench.get("@itempower") is not None:
                    meta[emid]["ip"] = _num(ench.get("@itempower"))
                ereq = _pick_requirement(_as_list(ench.get("craftingrequirements")))
                if ereq:
                    recipes[emid] = {
                        "res": _resources(ereq), "n": int(_num(ereq.get("@amountcrafted"), 1)) or 1,
                        "silver": _num(ereq.get("@silver")), "focus": _num(ereq.get("@craftingfocus")),
                        "kind": "craft",
                    }
                for up in _as_list((ench.get("upgraderequirements") or {}).get("upgraderesource")):
                    steps.append([lvl, up.get("@uniquename"), int(_num(up.get("@count")))])
            if steps:
                upgrades[uid] = sorted(steps)

            if category == "journalitem" and it.get("lootlist"):
                loot = [[market_id(x.get("@itemname"), x.get("@itemenchantmentlevel", 0)),
                         _num(x.get("@weight")), _num(x.get("@itemamount"), 1)]
                        for x in _as_list(it["lootlist"].get("loot")) if x.get("@itemname")]
                kind = uid.split("_JOURNAL_", 1)[1] if "_JOURNAL_" in uid else uid
                journals[uid] = {"t": info["t"], "type": kind, "fame": _num(it.get("@maxfame")),
                                 "base": _num(it.get("@baselootamount")), "loot": loot,
                                 "empty": f"{uid}_EMPTY", "full": f"{uid}_FULL"}

            if category == "farmableitem":
                silver = _num((it.get("craftingrequirements") or {}).get("@silver")) \
                    if isinstance(it.get("craftingrequirements"), dict) else 0.0
                harvest = it.get("harvest") if isinstance(it.get("harvest"), dict) else None
                grown = it.get("grownitem") if isinstance(it.get("grownitem"), dict) else None
                if it.get("@kind") == "plant" and harvest:
                    seed = harvest.get("seed") or {}
                    plants.append({
                        "seed": uid, "t": info["t"], "loot": harvest.get("@lootlist"),
                        "grow": _num(harvest.get("@growtime")), "fame": _num(harvest.get("@fame")),
                        "seed_chance": _num(seed.get("@chance")), "focus_bonus": _num(it.get("@activefarmbonus")),
                        "silver": silver,
                    })
                elif it.get("@kind") == "animal" and grown:
                    off = grown.get("offspring") or {}
                    animals.append({
                        "baby": uid, "grown": grown.get("@uniquename"), "t": info["t"],
                        "grow": _num(grown.get("@growtime")), "fame": _num(grown.get("@fame")),
                        "offspring_chance": _num(off.get("@chance")),
                        "focus_bonus": _num(it.get("@activefarmbonus")), "silver": silver,
                    })

    # Таблицы лута урожая.
    lootlists = {}
    if raw_loot:
        root = raw_loot.get("LootDefinition", raw_loot)
        for ll in _as_list(root.get("Lootlist")):
            name = ll.get("@name")
            entries = []
            for x in _as_list(ll.get("Item")):
                amount = str(x.get("@amount", "1"))
                lo, _, hi = amount.partition("-")
                avg = (_num(lo) + _num(hi or lo)) / 2
                entries.append([x.get("@type"), _num(x.get("@chance"), 1), avg])
            if name:
                lootlists[name] = entries
    for p in plants:
        p["yield"] = lootlists.get(p.pop("loot"), [])

    # Бонусы городов.
    city = {}
    if raw_modifiers:
        root = raw_modifiers.get("craftingmodifiers", raw_modifiers)
        for loc in _as_list(root.get("craftinglocation")):
            key = CLUSTER_TO_MARKET.get(loc.get("@clusterid"))
            if not key:
                continue
            city[key] = {
                "craft": _num((loc.get("craftingbonus") or {}).get("@value"), 0.18),
                "refine": _num((loc.get("refiningbonus") or {}).get("@value"), 0.18),
                "mods": {m.get("@name"): _num(m.get("@value")) for m in _as_list(loc.get("craftingmodifier"))},
            }

    # Ценность предметов без @itemvalue — сумма ценности ресурсов рецепта.
    def value(mid: str, depth: int = 0) -> float:
        info = meta.get(mid)
        if info is None:
            return 0.0
        if "v" in info:
            return info["v"]
        rec = recipes.get(mid)
        if not rec or depth > 5:
            return 0.0
        v = sum(value(r, depth + 1) * c for r, c, _ in rec["res"]) / rec["n"]
        info["v"] = v
        return v
    for mid in list(recipes):
        value(mid)

    return {"version": GAMEDATA_VERSION, "items": meta, "recipes": recipes, "upgrades": upgrades,
            "journals": journals, "plants": plants, "animals": animals, "cities": city,
            "zones": parse_world(world_text or ""), "destiny": parse_destiny(raw_achievements),
            "clusters": parse_clusters(raw_world)}


class GameData:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.version: int = int(data.get("version") or 1)
        self.items: dict = data.get("items", {})
        self.recipes: dict = data.get("recipes", {})
        for mid, rec in self.recipes.items():  # файлы старых версий могли не различать трансмутацию
            rec["kind"] = recipe_kind(data.get("items", {}).get(mid, {}), rec)
        self.upgrades: dict = data.get("upgrades", {})
        self.journals: dict = data.get("journals", {})
        self.plants: list = data.get("plants", [])
        self.animals: list = data.get("animals", [])
        self.cities: dict = data.get("cities", {})
        self.zones: dict = data.get("zones", {})
        self.destiny: dict = data.get("destiny") or {"templates": {}, "nodes": {}}
        self.clusters: dict = data.get("clusters", {})

    @classmethod
    def load(cls, path: str | Path) -> "GameData":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def __bool__(self) -> bool:
        return bool(self.recipes)

    def item_value(self, mid: str) -> float:
        return float((self.items.get(mid) or {}).get("v", 0.0))

    def city_bonus(self, city: str, item_id: str, refine: bool) -> float:
        """Бонус производства города для предмета (без фокуса), доля."""
        c = self.cities.get(city)
        if not c:
            return 0.18 if city else 0.0
        base = c["refine"] if refine else c["craft"]
        cat = (self.items.get(item_id) or {}).get("cc", "")
        return base + c["mods"].get(cat, 0.0)


    def cluster(self, cid: str) -> list | None:
        """[название, тип, соседи] кластера или None."""
        return self.clusters.get(cid)

    def cluster_type(self, cid: str) -> str:
        c = self.clusters.get(cid)
        return c[1] if c else ""

    def cluster_name(self, cid: str) -> str:
        c = self.clusters.get(cid)
        return (c[0] if c else "") or self.zones.get(cid) or cid


def download(path: str | Path, base_url: str = DUMPS_URL) -> dict:
    def fetch(name, optional=False):
        try:
            with urllib.request.urlopen(base_url + name, timeout=300) as resp:
                return json.load(resp)
        except (OSError, ValueError):
            if optional:  # без Доски судьбы и карты остальное работает
                return None
            raise
    with urllib.request.urlopen(base_url + "formatted/world.txt", timeout=180) as resp:
        world = resp.read().decode("utf-8", errors="replace")
    data = build(fetch("items.json"), fetch("loot.json"), fetch("craftingmodifiers.json"), world,
                 fetch("achievements.json", optional=True), fetch("cluster/world.json", optional=True))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"recipes": len(data["recipes"]), "journals": len(data["journals"]),
            "plants": len(data["plants"]), "animals": len(data["animals"]),
            "destiny_nodes": len(data["destiny"]["nodes"]), "clusters": len(data["clusters"])}
