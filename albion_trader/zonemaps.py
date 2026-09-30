"""Фоны карт зон для радара — из раскладок уровней игры (ao-bin-dumps).

Раскладка зоны: ``cluster/<файл>.cluster.xml`` перечисляет шаблоны уровня
(``templateinstance``) с позицией, поворотом и включёнными слоями, а
``templates/<GREEN|RED|DEAD|NONE>/<имя>.template.xml`` — тайлы шаблона:
земля с высотой, вода, дороги, скалы, постройки, деревья. Из этого строится
схема зоны в тех же координатах, что приходят в событиях (x, z движка → x, y
радара); выходы и их цели берутся из ``cluster/world.json``.

Файлы качаются по мере надобности — когда вы входите в зону — и хранятся в
``data/zonemaps``: готовая схема зоны (``<id>.json``) и разобранные шаблоны
(``templates/``), которые повторяются в разных зонах. Команда
``update-maps`` скачивает схемы заранее (по умолчанию — городов и всех зон,
где вы уже бывали).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

from .gamedata import DUMPS_URL

log = logging.getLogger("albion_trader.zonemaps")

ZONEMAP_VERSION = 3
TEMPLATE_FOLDERS = ("GREEN", "RED", "DEAD", "NONE")
MAX_TILES = 40000            # на зону; лишнее (мелочь) отбрасывается

# Категории тайлов по ключевым словам имени (порядок важен: первое совпадение).
# (категория, ключевые слова, размер по умолчанию: ширина, длина)
_SKIP = ("VEG_", "DECO", "LANTERN", "BANNER", "BUNTING", "HALLOWEEN", "XMAS", "EASTER", "ANNIVERSARY",
         "FACTION_GUARD", "LIGHT", "FX_", "SOUND", "PARTICLE", "SPAWN", "GUARD", "ATMOSPHEREBUBBLE", "DEBRIS")
# Имена тайлов начинаются с биома и «цвета» зоны (SWAMP_RED_…, ROADS_…, FOREST_RED_…) —
# их отрезаем, иначе ROADS_GROUND выглядел бы как дорога, а FOREST_GROUND — как лес.
_BIOMES = {"FOREST", "SWAMP", "HIGHLAND", "HIGHLANDS", "STEPPE", "MOUNTAIN", "MOUNTAINS", "ROADS", "MISTS", "MIST",
           "HELL", "AVALON", "CORRUPTED", "UNDEAD", "KEEPER", "MORGANA", "HERETIC", "ISLAND"}
_COLORS = {"RED", "GREEN", "BLACK", "DEAD", "YELLOW", "BLUE", "AVA", "NONE", "RO"}
_RULES = [
    ("water", ("WATER", "RIVER", "LAKE", "OCEAN", "FORD", "SWAMP_POND", "POND", "FISHINGZONE"), (10, 10)),
    ("road", ("ROAD", "STREET", "BRIDGE", "PATH_"), (10, 10)),
    ("plot", ("CONSTRUCTION", "REALESTATE"), (10, 10)),          # участки под постройки игроков
    ("building", ("CITYWALL", "HOUSE", "BUILDING", "BANK", "MARKETPLACE", "TOWER", "CASTLE",
                  "CITY_", "_HALL", "FORT", "WALL_WOOD", "STALL", "TENT"), (8, 8)),
    ("cliff", ("PLATEAU", "CLIFF", "BLOCKER", "MOUNTAIN", "HILL", "WALL", "CANYON", "RIDGE"), (8, 8)),
    ("tree", ("TREE", "FOREST", "BUSH", "WOOD_"), (4, 4)),
    ("rock", ("ROCK", "STONE", "BOULDER", "ORE"), (3, 3)),
    ("ground", ("GROUND", "SUB_", "MOBCAMP", "CAMP", "BBS_", "_FLAT", "FLOOR", "TERRAIN", "FLY_ISLAND"), (10, 10)),
]
_SIZE = re.compile(r"(\d{1,3})\s*[xX]\s*(\d{1,3})")
_LEN = re.compile(r"_(\d{1,2})M(?:_|$)")
_GENERIC_GROUND = re.compile(r"_\d{1,3}[xX]\d{1,3}")   # _SWAMP_RED_10x10_B и т. п.


def classify(name: str) -> tuple[str, float, float] | None:
    """Категория и размер тайла по имени или None (мелкий декор — не рисуем)."""
    tokens = name.upper().lstrip("_").split("_")
    while len(tokens) > 1 and (tokens[0] in _BIOMES or tokens[0] in _COLORS):
        tokens.pop(0)
    up = "_" + "_".join(tokens)
    if any(s in up for s in _SKIP) and "WATER" not in up and "RIVER" not in up:
        return None
    cat = None
    size = (0.0, 0.0)
    for c, words, default in _RULES:
        if any(w in up for w in words):
            cat, size = c, default
            break
    if cat is None:
        if _GENERIC_GROUND.search(up):
            cat, size = "ground", (10, 10)
        else:
            return None
    m = _SIZE.search(up)
    if m:
        size = (float(m.group(1)), float(m.group(2)))
    else:
        lm = _LEN.search(up)
        if lm and cat == "cliff":
            size = (float(lm.group(1)) if "STRAIGHT" in up else size[0], size[1])
            if "20M" in up:
                size = (20.0, 4.0)
    if cat in ("tree", "rock"):   # одиночные объекты, а не заливка
        size = (min(size[0], 12.0), min(size[1], 12.0))
    return cat, float(size[0]), float(size[1])


def _floats(s: str | None, n: int) -> list[float]:
    try:
        vals = [float(v) for v in (s or "").split()]
    except ValueError:
        vals = []
    return (vals + [0.0] * n)[:n]


def parse_template(xml_text: str) -> dict:
    """Шаблон уровня → тайлы с номерами слоёв и выходы."""
    root = ET.fromstring(xml_text)
    layers: list[str] = []
    layer_index: dict[str, int] = {}
    included: set[int] = set()

    def lidx(lid: str | None, name: str = "") -> int:
        if not lid:
            return -1
        if lid not in layer_index:
            layer_index[lid] = len(layers)
            layers.append(lid)
        i = layer_index[lid]
        if name.strip().lower() == "included":
            included.add(i)
        return i

    tiles, exits = [], []

    def add_tile(el, layer: int) -> None:
        name = el.get("name") or ""
        x, y, z = _floats(el.get("pos"), 3)
        rot = _floats(el.get("rot"), 3)[1] if el.get("rot") else float(el.get("roty") or 0)
        ex = el.find("exit")
        if ex is not None:
            exits.append([round(x, 1), round(z, 1), ex.get("minimapicon") or "", layer])
            return
        c = classify(name)
        if not c:
            return
        cat, w, h = c
        scale = _floats(el.get("scale"), 3) if el.get("scale") else None
        if scale and scale[0] > 0 and scale[2] > 0:
            w, h = w * scale[0], h * scale[2]
        tiles.append([cat, round(x, 1), round(z, 1), round(w, 1), round(h, 1), round(rot) % 360, round(y, 1), layer])

    for cm in root.iter("criticalscenemember"):
        layer = lidx(cm.get("layerId"))
        for t in cm.iter("tile"):
            add_tile(t, layer)
    tiles_root = root.find("tiles")
    if tiles_root is not None:
        def walk(el, layer: int) -> None:
            for child in el:
                if child.tag == "layer":
                    flagged = bool(child.get("flags"))
                    li = lidx(child.get("id"), child.get("name") or "")
                    walk(child, -2 if flagged and li not in included else li)
                elif child.tag in ("tile", "compoundtile"):
                    if layer != -2:
                        add_tile(child, layer)
                else:
                    walk(child, layer)
        walk(tiles_root, -1)
    bmin = _floats(root.get("editorBoundsMin"), 2)
    bmax = _floats(root.get("editorBoundsMax"), 2)
    return {"layers": layers, "included": sorted(included), "tiles": tiles, "exits": exits,
            "bounds": bmin + bmax}


def _rotate(x: float, z: float, deg: float) -> tuple[float, float]:
    """Поворот вокруг вертикальной оси, как в Unity (+90°: (1, 0) → (0, −1))."""
    if not deg:
        return x, z
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + z * s, -x * s + z * c


def parse_cluster(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    inst = []
    for ti in root.iter("templateinstance"):
        x, y, z = _floats(ti.get("pos"), 3)
        rot = _floats(ti.get("rot"), 3)[1] if len((ti.get("rot") or "").split()) == 3 else float(ti.get("rot") or 0)
        inst.append({"ref": ti.get("ref") or "", "x": x, "z": z, "rot": rot,
                     "active": [a.get("id") for a in ti.iter("activelayer") if a.get("id")]})
    return {"instances": inst,
            "bounds": _floats(root.get("minimapBoundsMin"), 2) + _floats(root.get("minimapBoundsMax"), 2),
            "origin": _floats(root.get("origin"), 2), "size": _floats(root.get("size"), 2),
            "height": _floats(root.get("minimapHeightRange"), 2)}


def assemble(cluster: dict, templates: dict[str, dict]) -> dict:
    """Собрать схему зоны из шаблонов: тайлы в координатах зоны."""
    tiles, exits = [], []
    for inst in cluster["instances"]:
        t = templates.get(inst["ref"])
        if not t:
            continue
        active = set(inst["active"])
        ok = {i for i, lid in enumerate(t["layers"]) if lid in active} | set(t.get("included") or [])
        for cat, x, z, w, h, rot, y, layer in t["tiles"]:
            if layer >= 0 and layer not in ok:
                continue
            wx, wz = _rotate(x, z, inst["rot"])
            tiles.append([cat, round(wx + inst["x"], 1), round(wz + inst["z"], 1), w, h,
                          round(rot + inst["rot"]) % 360, y])
        for x, z, icon, layer in t["exits"]:
            if layer >= 0 and layer not in ok:
                continue
            wx, wz = _rotate(x, z, inst["rot"])
            exits.append([round(wx + inst["x"], 1), round(wz + inst["z"], 1), icon])
    if len(tiles) > MAX_TILES:   # сначала крупное: земля, вода, дороги, скалы, постройки
        order = {"ground": 0, "water": 1, "road": 2, "cliff": 3, "building": 4, "plot": 5, "rock": 6, "tree": 7}
        tiles.sort(key=lambda t: order.get(t[0], 9))
        tiles = tiles[:MAX_TILES]
    b = cluster["bounds"]
    if not any(b):
        o, s = cluster["origin"], cluster["size"]
        b = [o[0], o[1], o[0] + s[0], o[1] + s[1]]
    return {"bounds": b, "height": cluster["height"], "tiles": tiles, "exits": exits}


def parse_world_index(raw: dict) -> dict:
    """cluster/world.json → {id: {file, type, name, exits: [[x, y, цель, значок]]}}."""
    root = next((v for k, v in raw.items() if not k.startswith("?") and isinstance(v, dict)), raw)
    clusters = (root.get("clusters") or {}).get("cluster") or []
    if isinstance(clusters, dict):
        clusters = [clusters]
    out = {}
    for c in clusters:
        cid, f = c.get("@id"), c.get("@file")
        if not cid or not f:
            continue
        exits = (c.get("exits") or {}).get("exit") or []
        if isinstance(exits, dict):
            exits = [exits]
        ex = []
        for e in exits:
            if not isinstance(e, dict):
                continue
            pos = _floats(e.get("@pos"), 2)
            target = (e.get("@targetid") or "").rsplit("@", 1)[-1]
            ex.append([pos[0], pos[1], target, e.get("@minimapicon") or ""])
        out[cid] = {"file": f, "type": c.get("@type", ""), "name": c.get("@displayname") or cid, "exits": ex}
    return out


def _folders_for(ctype: str) -> tuple[str, ...]:
    """Папки шаблонов: сначала подходящая к типу зоны (цвет), потом остальные."""
    t = ctype.upper()
    first = "DEAD" if "BLACK" in t else "RED" if "RED" in t else "GREEN"
    return (first,) + tuple(f for f in TEMPLATE_FOLDERS if f != first)


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)[:120]


class ZoneMaps:
    """Схемы зон: кэш на диске, фоновая загрузка при входе в зону."""

    def __init__(self, data_dir: str | Path, base_url: str = DUMPS_URL,
                 fetch: Callable[[str], bytes] | None = None,
                 name_of: Callable[[str], str] | None = None):
        self.dir = Path(data_dir)
        self.base_url = base_url.rstrip("/") + "/"
        self.fetch = fetch or self._http
        self.name_of = name_of or (lambda cid: cid)
        self.lock = threading.Lock()
        self.loading: dict[str, threading.Thread] = {}
        self.errors: dict[str, str] = {}
        self._index: dict | None = None

    # --- сеть ------------------------------------------------------------
    def _http(self, path: str) -> bytes:
        with urllib.request.urlopen(self.base_url + path, timeout=120) as resp:
            return resp.read()

    # --- индекс зон --------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    def save_index(self, raw_world: dict) -> int:
        idx = parse_world_index(raw_world)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        self._index = idx
        return len(idx)

    def index(self) -> dict:
        if self._index is None:
            if self.index_path.exists():
                try:
                    self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._index = None
            if self._index is None:
                log.info("Скачиваю список зон (cluster/world.json)…")
                self.save_index(json.loads(self.fetch("cluster/world.json")))
        return self._index

    def zone_name(self, cid: str) -> str:
        """Название зоны: из справочника программы, иначе из cluster/world.json."""
        if not cid:
            return ""
        name = self.name_of(cid)
        if name and name not in (cid, "—"):
            return name
        if self._index is None and not self.index_path.exists():
            return name or cid      # списка зон ещё нет — не качаем его ради названия
        return (self.index().get(cid) or {}).get("name") or cid

    # --- шаблоны ---------------------------------------------------------------
    def template(self, ref: str, ctype: str) -> dict | None:
        tdir = self.dir / "templates"
        for folder in _folders_for(ctype):
            p = tdir / f"{folder}_{_safe(ref)}.json"
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
        for folder in _folders_for(ctype):
            try:
                text = self.fetch(f"templates/{folder}/{ref}.template.xml")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
            t = parse_template(text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / f"{folder}_{_safe(ref)}.json").write_text(json.dumps(t, separators=(",", ":")), encoding="utf-8")
            return t
        log.warning("Шаблон %s не найден", ref)
        return None

    # --- схема зоны --------------------------------------------------------------
    def path(self, cid: str) -> Path:
        return self.dir / f"{_safe(cid)}.json"

    def cached(self, cid: str) -> dict | None:
        p = self.path(cid)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if data.get("version") == ZONEMAP_VERSION else None

    def build(self, cid: str) -> dict:
        info = self.index().get(cid)
        if not info:
            raise KeyError(f"зоны {cid} нет в cluster/world.json")
        text = self.fetch("cluster/" + info["file"])
        cluster = parse_cluster(text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text)
        templates = {}
        for ref in {i["ref"] for i in cluster["instances"]}:
            t = self.template(ref, info["type"])
            if t:
                templates[ref] = t
        data = assemble(cluster, templates)
        data["exits_world"] = [[x, y, target, icon, self.zone_name(target)]
                               for x, y, target, icon in info.get("exits", [])]
        data.update(version=ZONEMAP_VERSION, id=cid, name=info["name"], type=info["type"])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path(cid).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log.info("Схема зоны %s (%s): %d тайлов", cid, info["name"], len(data["tiles"]))
        return data

    def get(self, cid: str, background: bool = True) -> dict:
        """Схема зоны или статус загрузки: {"status": ready|loading|error|unknown}."""
        if not cid:
            return {"status": "unknown"}
        data = self.cached(cid)
        if data:
            return {"status": "ready", **data}
        with self.lock:
            if cid in self.errors:
                return {"status": "error", "id": cid, "error": self.errors[cid]}
            th = self.loading.get(cid)
            if th and th.is_alive():
                return {"status": "loading", "id": cid}
            if background:
                th = threading.Thread(target=self._load, args=(cid,), daemon=True)
                self.loading[cid] = th
                th.start()
                return {"status": "loading", "id": cid}
        self._load(cid)
        return self.get(cid, background=False)   # теперь либо в кэше, либо в errors

    def _load(self, cid: str) -> None:
        try:
            self.build(cid)
        except KeyError as e:
            with self.lock:
                self.errors[cid] = str(e.args[0])
        except (OSError, ValueError, ET.ParseError) as e:
            log.warning("Не удалось построить схему зоны %s: %s", cid, e)
            with self.lock:
                self.errors[cid] = f"не удалось скачать: {e}"
        finally:
            with self.lock:
                self.loading.pop(cid, None)

    def retry(self, cid: str) -> None:
        with self.lock:
            self.errors.pop(cid, None)

    def prefetch(self, ids: Iterable[str], progress: Callable[[str, int, int, str], None] | None = None) -> dict:
        ids = [i for i in dict.fromkeys(ids) if i]
        done = failed = 0
        for n, cid in enumerate(ids, 1):
            if self.cached(cid):
                done += 1
                status = "есть"
            else:
                try:
                    self.build(cid)
                    done += 1
                    status = "скачано"
                except (KeyError, OSError, ValueError, ET.ParseError) as e:
                    failed += 1
                    status = f"ошибка: {e}"
            if progress:
                progress(cid, n, len(ids), status)
        return {"ok": done, "failed": failed, "total": len(ids)}
