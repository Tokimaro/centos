"""Радар: игроки, мобы, ресурсы и лут вокруг персонажа по событиям Photon.

События ``New…`` добавляют объект, ``Move``/``Teleport`` двигают, ``Leave`` и
пустой ``HarvestableChangeState`` удаляют. Своя позиция — из ответа Join
(параметр 9) и своего запроса Move (параметр 1); смена зоны очищает карту.
Любое другое событие ``New…``/``…Object`` с координатами (сундуки, серебро,
порталы) попадает на карту как «объект» — если его код известен (``names``).

Координаты — массив из двух float или байтовый блок Move (float32 LE по
смещениям 9 и 13). Ключи параметров для каждого события можно переопределить
файлом ``data/radar.json``: ``{"params": {"new_mob": {"position": [8]}}}``.

Движение — самое частое событие игры, поэтому оно разбирается, только пока
радар кто-то смотрит (запросы ``/api/radar`` не реже раза в ``ACTIVE_WINDOW`` с).
"""

from __future__ import annotations

import copy
import json
import logging
import math
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .radar_data import CodeGuesser, MobTable, player_power

log = logging.getLogger("albion_trader.radar")

ACTIVE_WINDOW = 30.0
MAX_CODES = 400           # сколько разных кодов событий держать в диагностике
STALE_AFTER = 30 * 60     # объект без обновлений дольше — убираем
DEPLETED_KEEP = 2 * 3600  # сколько помнить истощённые узлы (для таймеров респауна)
ENCOUNTER_GAP = 10 * 60   # одна встреча с игроком — не чаще раза в 10 минут

SCHEMA = """
CREATE TABLE IF NOT EXISTS radar_players (
    name        TEXT PRIMARY KEY,
    guild       TEXT,
    alliance    TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    times       INTEGER NOT NULL DEFAULT 0,
    hostile     INTEGER NOT NULL DEFAULT 0,
    last_zone   TEXT,
    zones       TEXT
);
CREATE TABLE IF NOT EXISTS radar_nodes (
    zone     TEXT    NOT NULL,
    gx       INTEGER NOT NULL,
    gy       INTEGER NOT NULL,
    res      TEXT    NOT NULL,
    tier     INTEGER NOT NULL,
    enchant  INTEGER NOT NULL,
    seen     INTEGER NOT NULL DEFAULT 0,
    last     INTEGER NOT NULL,
    PRIMARY KEY (zone, gx, gy, res, tier, enchant)
);
"""
HEAT_CELL = 10            # клетка тепловой карты ресурсов, м


def init(conn) -> None:
    conn.executescript(SCHEMA)

KIND_PLAYER, KIND_MOB, KIND_RESOURCE, KIND_LOOT, KIND_OBJECT = "player", "mob", "resource", "loot", "object"
LOOT_EVENTS = {"new_loot", "new_loot_chest", "new_treasure_chest", "new_silver_object"}
GENERIC_NAMES = {"new_loot": "лут", "new_loot_chest": "сундук", "new_treasure_chest": "сундук с сокровищами",
                 "new_silver_object": "серебро"}

# Ключи параметров по событиям. "position" — кандидаты по порядку; если ни один
# не похож на координаты, позиция ищется среди всех параметров.
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "leave": {"id": 0},
    "move": {"id": 0, "position": [1, 4]},
    "teleport": {"id": 0, "position": [1, 2]},
    "new_character": {"id": 0, "name": 1, "guild": 8, "alliance": 49, "faction": 53,
                      "position": [12, 13, 14], "health": 22, "max_health": 23, "equipment": 40},
    "character_equipment_changed": {"id": 0, "equipment": 2},
    "regeneration_health_changed": {"id": 0, "health": 2, "max_health": 3},
    "mounted": {"id": 0, "mounted": 11, "mounted_alt": 10},
    "change_flagging_finished": {"id": 0, "faction": 1},
    "new_mob": {"id": 0, "type_id": 1, "position": [7, 8], "health": 13, "max_health": 14,
                "rarity": 19, "name": 32, "enchant": 33},
    "mob_change_state": {"id": 0, "enchant": 1},
    "health_update": {"id": 0, "health": 3},
    "new_harvestable_object": {"id": 0, "type": 5, "tier": 7, "position": [8], "size": 10, "enchant": 11},
    "new_simple_harvestable_object_list": {"ids": 0, "types": 1, "tiers": 2, "positions": 3, "sizes": 4},
    "harvestable_change_state": {"id": 0, "size": 1, "enchant": 2},
    "new_loot_chest": {"id": 0, "name": 3, "rarity": 21, "rarity_static": 23},
    "loot_chest_opened": {"id": 0},
    "_generic": {"id": 0},
    "op:join": {"id": 0, "name": 2, "position": [9]},
    "op:move": {"position": [1]},
}

# Тип ресурса NewHarvestableObject (HarvestableType) → вид, по диапазонам.
RESOURCE_TYPES = [(0, 5, "wood", "дерево"), (6, 10, "rock", "камень"), (11, 15, "fiber", "волокно"),
                  (16, 22, "hide", "шкура"), (23, 27, "ore", "руда")]


def resource_kind(type_id) -> tuple[str, str]:
    if isinstance(type_id, int):
        for lo, hi, key, name in RESOURCE_TYPES:
            if lo <= type_id <= hi:
                return key, name
    return "other", "ресурс"


@dataclass
class Entity:
    id: int
    kind: str
    x: float = 0.0
    y: float = 0.0
    name: str = ""
    event: str = ""
    type_id: int | None = None
    res: str = ""                 # вид ресурса: wood/rock/fiber/hide/ore
    tier: int | None = None
    enchant: int | None = None
    size: int | None = None
    guild: str = ""
    alliance: str = ""
    faction: int | None = None
    health: float | None = None
    max_health: float | None = None
    mounted: bool | None = None
    rarity: int | None = None     # моб: редкость; сундук: 0–3
    opened: bool = False          # сундук открыт
    equipment: list = field(default_factory=list)   # индексы предметов (AlbionId)
    updated: float = 0.0


# PvP-флаг игрока (NewCharacter[53]): 0 — без флага, 1–6 — фракция города, 255 — враждебный.
FACTIONS = {0: "", 1: "Мартлок", 2: "Лимхерст", 3: "Бриджвотч", 4: "Форт Стерлинг", 5: "Тетфорд",
            6: "Карлеон", 255: "враждебный"}
# Слоты снаряжения в NewCharacter[40] по порядку.
EQUIPMENT_SLOTS = ("оружие", "вторая рука", "голова", "броня", "обувь", "сумка", "плащ", "маунт", "зелье", "еда")


def _equipment(v) -> list:
    return [(_int(x) or 0) for x in _seq(v)][:len(EQUIPMENT_SLOTS)]


# --- значения из параметров ------------------------------------------------
def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _seq(v) -> list:
    if isinstance(v, (bytes, bytearray)):
        return list(v)
    return list(v) if isinstance(v, (list, tuple)) else []


def as_position(v) -> tuple[float, float] | None:
    """[x, y] из float или байтовый блок Move (float32 LE по смещениям 9 и 13)."""
    if isinstance(v, (list, tuple)) and len(v) >= 2 and all(isinstance(c, float) for c in v[:2]):
        x, y = v[0], v[1]
    elif isinstance(v, (bytes, bytearray)) and len(v) >= 17:
        x, y = struct.unpack_from("<ff", v, 9)
    else:
        return None
    if math.isfinite(x) and math.isfinite(y) and abs(x) < 1e5 and abs(y) < 1e5:
        return float(x), float(y)
    return None


def find_position(params: dict, candidates=None) -> tuple[float, float] | None:
    keys = candidates if isinstance(candidates, list) else ([] if candidates is None else [candidates])
    for k in keys:
        pos = as_position(params.get(k))
        if pos:
            return pos
    for k in sorted(k for k in params if isinstance(k, int) and k < 250):
        if isinstance(params[k], (list, tuple)):   # байтовый блок — только по явному ключу
            pos = as_position(params[k])
            if pos:
                return pos
    return None


def describe(v) -> str:
    """Короткое описание значения для диагностики кодов."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (bytes, bytearray)):
        return f"bytes[{len(v)}]" + ("·pos" if 17 <= len(v) <= 40 and as_position(v) else "")
    if isinstance(v, (list, tuple)):
        return f"list[{len(v)}]" + ("·pos" if len(v) == 2 and as_position(v) else "")
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


# --- радар -----------------------------------------------------------------
class Radar:
    def __init__(self, params_path: str | Path | None = None, clock: Callable[[], float] = time.time,
                 item_of: Callable[[int], str | None] | None = None,
                 item_name: Callable[[str], str] | None = None,
                 zone_type: Callable[[str], str] | None = None,
                 item_ip: Callable[[str], float | None] | None = None,
                 mobs: MobTable | None = None):
        self.clock = clock
        self.item_of = item_of or (lambda _i: None)
        self.item_name = item_name or (lambda iid: iid)
        self.zone_type = zone_type or (lambda _z: "")
        self.item_ip = item_ip or (lambda _iid: None)
        self.mobs = mobs
        self.mob_offset = 0
        self.depleted: dict[str, list[dict]] = {}     # зона → истощённые узлы
        self.pending_players: list[dict] = []          # встречи для записи в базу
        self.pending_nodes: list[tuple] = []           # узлы ресурсов для тепловой карты
        self._recent_players: dict[str, float] = {}
        self.guesser = CodeGuesser()
        self.lock = threading.RLock()
        self.entities: dict[int, Entity] = {}
        self.me = {"id": None, "name": "", "x": 0.0, "y": 0.0, "zone": ""}
        self.params = copy.deepcopy(DEFAULT_PARAMS)
        self.codes: dict[int, dict] = {}     # диагностика: код события → число и форма
        self.state = None
        if params_path and Path(params_path).exists():
            try:
                extra = json.loads(Path(params_path).read_text(encoding="utf-8")).get("params") or {}
                for name, keys in extra.items():
                    self.params.setdefault(name, {}).update(keys)
                self.mob_offset = int(json.loads(Path(params_path).read_text(encoding="utf-8")).get("mob_offset") or 0)
                log.info("Ключи параметров радара переопределены из %s", params_path)
            except (ValueError, OSError, AttributeError) as e:
                log.warning("Не удалось прочитать %s: %s", params_path, e)

    def keys(self, name: str) -> dict:
        return self.params.get(name) or self.params["_generic"]

    # --- подключение к сборщику ------------------------------------------
    def attach(self, state) -> None:
        self.state = state
        for name in state.ev:
            state.on("event:" + name, lambda p, n=name: self.on_event(n, p))
        state.on("event", self.on_raw_event)
        state.on("response:join", self.on_join)
        state.on("request:move", self.on_own_move)
        state.on("zone", lambda zone, _prev: self.on_zone(zone))

    def touch(self) -> None:
        """Радар смотрят: разбирать движение ещё ACTIVE_WINDOW секунд."""
        if self.state is not None:
            self.state.moves_until = self.clock() + ACTIVE_WINDOW

    # --- обработчики -----------------------------------------------------
    def on_raw_event(self, code: int, params: dict) -> None:
        with self.lock:
            c = self.codes.get(code)
            if c is None:
                if len(self.codes) >= MAX_CODES:
                    return
                c = self.codes[code] = {"count": 0, "shape": " ".join(
                    f"{k}:{describe(v)}" for k, v in sorted(params.items()) if isinstance(k, int) and k < 250)}
            c["count"] += 1
            c["last"] = self.clock()
            self.guesser.observe(code, params)

    def on_join(self, p: dict) -> None:
        keys = self.keys("op:join")
        with self.lock:
            self.entities.clear()
            oid = _int(p.get(keys["id"]))
            if oid is not None:
                self.me["id"] = oid
            self.me["name"] = _str(p.get(keys["name"])) or self.me["name"]
            pos = find_position(p, keys.get("position"))
            if pos:
                self.me["x"], self.me["y"] = pos

    def on_own_move(self, p: dict) -> None:
        pos = find_position(p, self.keys("op:move").get("position"))
        if pos:
            with self.lock:
                self.me["x"], self.me["y"] = pos

    def on_zone(self, zone: str) -> None:
        with self.lock:
            if zone != self.me["zone"]:
                self.entities.clear()
                self.me["zone"] = zone

    def on_event(self, name: str, p: dict) -> None:
        keys = self.keys(name)
        handler = getattr(self, "_ev_" + name, None)
        with self.lock:
            if handler:
                handler(p, keys)
            elif name.startswith("new_") or name.endswith("_object"):
                self._generic(name, p, keys)

    def _ev_leave(self, p, keys):
        self.entities.pop(_int(p.get(keys["id"])), None)

    def _ev_move(self, p, keys):
        eid = _int(p.get(keys["id"]))
        pos = find_position(p, keys.get("position"))
        if pos is None:
            return
        if eid is not None and eid == self.me["id"]:
            self.me["x"], self.me["y"] = pos
        ent = self.entities.get(eid)
        if ent:
            ent.x, ent.y = pos
            ent.updated = self.clock()

    _ev_teleport = _ev_move

    def _ev_new_character(self, p, keys):
        self._put(Entity(id=_int(p.get(keys["id"])), kind=KIND_PLAYER, event="new_character",
                         name=_str(p.get(keys.get("name"))), guild=_str(p.get(keys.get("guild"))),
                         alliance=_str(p.get(keys.get("alliance"))), faction=_int(p.get(keys.get("faction"))),
                         health=_num(p.get(keys.get("health"))), max_health=_num(p.get(keys.get("max_health"))),
                         equipment=_equipment(p.get(keys.get("equipment")))),
                  p, keys)
        ent = self.entities.get(_int(p.get(keys["id"])))
        if ent and ent.kind == KIND_PLAYER:
            self._remember_player(ent)

    def _remember_player(self, ent: Entity) -> None:
        if not ent.name or ent.id == self.me["id"]:
            return
        now = self.clock()
        if now - self._recent_players.get(ent.name, 0) < ENCOUNTER_GAP:
            return
        self._recent_players[ent.name] = now
        if len(self._recent_players) > 5000:
            self._recent_players.clear()
        self.pending_players.append({"name": ent.name, "guild": ent.guild, "alliance": ent.alliance,
                                     "hostile": ent.faction == 255, "zone": self.me["zone"], "ts": int(now)})

    def _player(self, p, keys) -> Entity | None:
        ent = self.entities.get(_int(p.get(keys["id"])))
        return ent if ent and ent.kind == KIND_PLAYER else None

    def _ev_character_equipment_changed(self, p, keys):
        ent = self._player(p, keys)
        if ent:
            ent.equipment = _equipment(p.get(keys.get("equipment")))

    def _ev_regeneration_health_changed(self, p, keys):
        ent = self.entities.get(_int(p.get(keys["id"])))
        if ent:
            hp, mx = _num(p.get(keys.get("health"))), _num(p.get(keys.get("max_health")))
            if hp is not None:
                ent.health = hp
            if mx:
                ent.max_health = mx

    def _ev_mounted(self, p, keys):
        """Маунт: параметр 11 — true, либо 10 == -1 (так делает ZQRadar)."""
        ent = self._player(p, keys)
        if ent:
            flag, alt = p.get(keys.get("mounted")), _int(p.get(keys.get("mounted_alt")))
            ent.mounted = flag is True or flag == "true" or alt == -1

    def _ev_change_flagging_finished(self, p, keys):
        ent = self._player(p, keys)
        faction = _int(p.get(keys.get("faction")))
        if ent and faction is not None:
            ent.faction = faction

    def _ev_new_mob(self, p, keys):
        self._put(Entity(id=_int(p.get(keys["id"])), kind=KIND_MOB, event="new_mob",
                         type_id=_int(p.get(keys.get("type_id"))), name=_str(p.get(keys.get("name"))),
                         enchant=_int(p.get(keys.get("enchant"))), health=_num(p.get(keys.get("health"))),
                         max_health=_num(p.get(keys.get("max_health"))), rarity=_int(p.get(keys.get("rarity")))),
                  p, keys)

    def _ev_mob_change_state(self, p, keys):
        ent = self.entities.get(_int(p.get(keys["id"])))
        if ent:
            ent.enchant = _int(p.get(keys.get("enchant")))

    def _ev_health_update(self, p, keys):
        ent = self.entities.get(_int(p.get(keys["id"])))
        hp = _num(p.get(keys.get("health")))
        if ent and hp is not None:
            ent.health = hp

    def _ev_new_harvestable_object(self, p, keys):
        type_id = _int(p.get(keys.get("type")))
        res, name = resource_kind(type_id)
        self._put(Entity(id=_int(p.get(keys["id"])), kind=KIND_RESOURCE, event="new_harvestable_object",
                         type_id=type_id, res=res, name=name, tier=_int(p.get(keys.get("tier"))),
                         size=_int(p.get(keys.get("size"))), enchant=_int(p.get(keys.get("enchant")))), p, keys)
        ent = self.entities.get(_int(p.get(keys["id"])))
        if ent and ent.kind == KIND_RESOURCE:
            self._node_seen(ent)

    def _ev_new_simple_harvestable_object_list(self, p, keys):
        ids, types = _seq(p.get(keys["ids"])), _seq(p.get(keys["types"]))
        tiers, sizes = _seq(p.get(keys["tiers"])), _seq(p.get(keys["sizes"]))
        positions = _seq(p.get(keys["positions"]))
        now = self.clock()
        for i, eid in enumerate(ids):
            if 2 * i + 1 >= len(positions) or _int(eid) is None:
                break
            x, y = _num(positions[2 * i]), _num(positions[2 * i + 1])
            if x is None or y is None:
                continue
            type_id = _int(types[i]) if i < len(types) else None
            res, name = resource_kind(type_id)
            ent = self.entities[int(eid)] = Entity(
                id=int(eid), kind=KIND_RESOURCE, x=float(x), y=float(y), event="new_simple_harvestable_object_list",
                type_id=type_id, res=res, name=name, tier=_int(tiers[i]) if i < len(tiers) else None,
                size=_int(sizes[i]) if i < len(sizes) else None, updated=now)
            self._node_seen(ent)

    def _ev_harvestable_change_state(self, p, keys):
        eid = _int(p.get(keys["id"]))
        ent = self.entities.get(eid)
        if not ent:
            return
        size = _int(p.get(keys.get("size")))
        if size is not None and size <= 0:
            del self.entities[eid]
            if ent.kind == KIND_RESOURCE:
                self._node_depleted(ent)
            return
        if size is not None:
            ent.size = size
        enchant = _int(p.get(keys.get("enchant")))
        if enchant is not None:
            ent.enchant = enchant

    def _node_seen(self, ent: Entity) -> None:
        if ent.res in ("", "other") or not ent.tier or not self.me["zone"]:
            return
        if len(self.pending_nodes) < 20000:
            self.pending_nodes.append((self.me["zone"], int(ent.x // HEAT_CELL), int(ent.y // HEAT_CELL), ent.res,
                                       ent.tier, ent.enchant or 0, int(self.clock())))
        # Узел снова появился — убрать его из истощённых.
        lst = self.depleted.get(self.me["zone"])
        if lst:
            lst[:] = [d for d in lst if (d["x"] - ent.x) ** 2 + (d["y"] - ent.y) ** 2 > 4]

    def _node_depleted(self, ent: Entity) -> None:
        lst = self.depleted.setdefault(self.me["zone"], [])
        lst.append({"x": ent.x, "y": ent.y, "res": ent.res, "name": ent.name, "tier": ent.tier,
                    "enchant": ent.enchant or 0, "at": self.clock()})
        del lst[:-300]

    def _ev_new_loot_chest(self, p, keys):
        self._generic("new_loot_chest", p, keys)
        ent = self.entities.get(_int(p.get(keys["id"])))
        if ent:
            rarity = _int(p.get(keys.get("rarity")))
            if rarity is None and ent.name.startswith("STATIC_"):
                rarity = _int(p.get(keys.get("rarity_static")))
            ent.rarity = rarity if rarity in (0, 1, 2, 3) else None

    def _ev_loot_chest_opened(self, p, keys):
        ent = self.entities.get(_int(p.get(keys["id"])))
        if ent:
            ent.opened = True

    def _generic(self, name: str, p, keys):
        label = _str(p.get(keys["name"])) if "name" in keys else ""
        if not label:
            label = next((v for k, v in sorted(p.items(), key=lambda kv: str(kv[0]))
                          if isinstance(k, int) and k < 250 and isinstance(v, str) and v), "")
        kind = KIND_LOOT if name in LOOT_EVENTS else KIND_OBJECT
        self._put(Entity(id=_int(p.get(keys["id"])), kind=kind, event=name,
                         name=label or GENERIC_NAMES.get(name) or name.removeprefix("new_").replace("_", " ")),
                  p, keys)

    def _put(self, ent: Entity, p, keys) -> None:
        pos = find_position(p, keys.get("position"))
        if ent.id is None or pos is None:
            return
        ent.x, ent.y = pos
        ent.updated = self.clock()
        self.entities[ent.id] = ent

    def _items(self, indices: list) -> list:
        out = []
        for slot, idx in zip(EQUIPMENT_SLOTS, indices):
            iid = self.item_of(idx) if idx > 0 else None
            if iid:
                out.append({"slot": slot, "id": iid, "name": self.item_name(iid)})
        return out

    # --- снимок для интерфейса ---------------------------------------------
    def snapshot(self, touch: bool = True) -> dict:
        if touch:
            self.touch()
        now = self.clock()
        with self.lock:
            for eid in [e.id for e in self.entities.values() if now - e.updated > STALE_AFTER]:
                del self.entities[eid]
            me = dict(self.me)
            ents = []
            for e in self.entities.values():
                d = asdict(e)
                d["dist"] = round(math.hypot(e.x - me["x"], e.y - me["y"]), 1)
                d["age"] = round(now - e.updated, 1)
                if e.kind == KIND_PLAYER:
                    d["equipment"] = self._items(e.equipment)
                    d["flag"] = FACTIONS.get(e.faction, "")
                    d.update(player_power(d["equipment"], self.item_ip))
                elif e.kind == KIND_MOB and self.mobs is not None:
                    d["mob"] = self.mobs.info(e.type_id, self.mob_offset)
                ents.append(d)
            depleted = [{**x, "ago": round(now - x["at"])} for x in self.depleted.get(me["zone"], [])
                        if now - x["at"] < DEPLETED_KEEP]
            me["zone_type"] = self.zone_type(me["zone"]) if me["zone"] else ""
            names = {v: k for k, v in (self.state.ev.items() if self.state else ())}
            codes = [{"code": c, "name": names.get(c), **info} for c, info in sorted(self.codes.items())]
        ents.sort(key=lambda d: d["dist"])
        return {"me": me, "entities": ents, "codes": codes, "now": now, "depleted": depleted,
                "suggestions": self.guesser.suggestions(dict(self.state.ev) if self.state else {}),
                "mob_offset": self.mob_offset,
                "moves": bool(self.state and now < self.state.moves_until)}

    # --- запись в базу (вызывается сервером) -----------------------------------
    def flush(self, conn) -> int:
        """Записать накопленные встречи с игроками и узлы ресурсов."""
        with self.lock:
            players, self.pending_players = self.pending_players, []
            nodes, self.pending_nodes = self.pending_nodes, []
        for pl in players:
            row = conn.execute("SELECT zones FROM radar_players WHERE name = ?", (pl["name"],)).fetchone()
            zones = json.loads(row[0] or "[]") if row else []
            if pl["zone"] and pl["zone"] not in zones:
                zones = (zones + [pl["zone"]])[-10:]
            conn.execute("""INSERT INTO radar_players(name, guild, alliance, first_seen, last_seen, times, hostile,
                                                      last_zone, zones)
                            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                            ON CONFLICT(name) DO UPDATE SET guild=excluded.guild, alliance=excluded.alliance,
                              last_seen=excluded.last_seen, times=times+1, hostile=hostile+excluded.hostile,
                              last_zone=excluded.last_zone, zones=excluded.zones""",
                         (pl["name"], pl["guild"], pl["alliance"], pl["ts"], pl["ts"], int(pl["hostile"]),
                          pl["zone"], json.dumps(zones, ensure_ascii=False)))
        for zone, gx, gy, res, tier, ench, ts in nodes:
            conn.execute("""INSERT INTO radar_nodes(zone, gx, gy, res, tier, enchant, seen, last)
                            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                            ON CONFLICT(zone, gx, gy, res, tier, enchant) DO UPDATE SET seen=seen+1, last=excluded.last""",
                         (zone, gx, gy, res, tier, ench, ts))
        return len(players) + len(nodes)


def history(conn, since: int, limit: int = 300) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM radar_players WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?", (since, limit))]
    for r in rows:
        r["zones"] = json.loads(r["zones"] or "[]")
    return rows


def heat(conn, zone: str) -> list[dict]:
    return [{"x": (r["gx"] + 0.5) * HEAT_CELL, "y": (r["gy"] + 0.5) * HEAT_CELL, "res": r["res"], "tier": r["tier"],
             "enchant": r["enchant"], "seen": r["seen"], "last": r["last"]}
            for r in conn.execute("SELECT * FROM radar_nodes WHERE zone = ?", (zone,))]


def kills(conn, names: list[str]) -> dict[str, dict]:
    """Убийства и смерти игроков по киллборду (если он включён и загружен)."""
    out: dict[str, dict] = {}
    if not names:
        return out
    try:
        marks = ",".join("?" * len(names))
        for name, k, last in conn.execute(
                f"SELECT killer, COUNT(*), MAX(ts) FROM kb_events WHERE killer IN ({marks}) GROUP BY killer", names):
            out.setdefault(name, {"kills": 0, "deaths": 0, "last": 0}).update(kills=k, last=max(last or 0, 0))
        for name, d, last in conn.execute(
                f"SELECT victim, COUNT(*), MAX(ts) FROM kb_events WHERE victim IN ({marks}) GROUP BY victim", names):
            e = out.setdefault(name, {"kills": 0, "deaths": 0, "last": 0})
            e["deaths"] = d
            e["last"] = max(e["last"], last or 0)
    except Exception:   # таблицы киллборда нет — нет и данных
        return {}
    return out
