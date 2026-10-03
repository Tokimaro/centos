"""Билд персонажа для боя: умения надетой экипировки и их перезарядки.

Экипировку бот узнаёт из трафика (событие смены экипировки своего персонажа),
умения предметов и перезарядки — из справочника игры (``spells.json`` из
ao-bin-dumps, скачивается вместе с остальными справочниками). Какое из умений
слота выбрано, игра не сообщает, поэтому вид и перезарядка берутся по всем
вариантам слота, а точная перезарядка уточняется в бою: после нажатия игра
отправляет запрос, только если умение готово (``KeyLearner``).
"""

from __future__ import annotations

import collections
from typing import NamedTuple

from .bot_core import Skill

# Слот экипировки (как в радаре) → клавиши его умений и раздел справочника.
WEAPON_KEYS = (("q", "q"), ("w", "w"), ("e", "e"))
SLOT_KEYS = {"оружие": WEAPON_KEYS, "броня": (("r", "a"),), "голова": (("d", "a"),), "обувь": (("f", "a"),)}
DEFAULT_CD = 10.0       # перезарядка, если в справочнике 0 (особые умения) или справочника нет
KIND_ORDER = ("heal", "shield", "buff", "cc", "damage", "move", "food")
KIND_NAMES = {"heal": "лечение", "shield": "щит", "buff": "усиление", "cc": "контроль", "damage": "урон",
              "move": "рывок", "food": "еда"}
AIM_TARGETS = ("enemy", "ground", "enemyplayers", "enemymobs")


class SlotSpell(NamedTuple):
    """Клавиша слота: предмет, варианты умений и как их применять."""
    key: str
    slot: str
    item: str
    spells: tuple
    cd: float           # перезарядка по справочнику (наименьшая из вариантов)
    cast: float
    kind: str
    aim: bool           # нужна точка/цель под курсором


def base_id(iid: str) -> str:
    return (iid or "").split("@")[0]


def slot_spell(key: str, slot: str, item: str, names: list, spells: dict, kind: str | None = None) -> SlotSpell:
    data = [spells[n] for n in names]
    cds = [float(x[0]) for x in data if float(x[0]) > 0]
    votes = collections.Counter(x[3] for x in data)
    best = kind or sorted(votes.items(), key=lambda kv: (-kv[1], KIND_ORDER.index(kv[0])
                                                         if kv[0] in KIND_ORDER else 99))[0][0]
    aim = sum(x[2] in AIM_TARGETS for x in data) * 2 > len(data)
    return SlotSpell(key, slot, item, tuple(names), min(cds) if cds else DEFAULT_CD,
                     min(float(x[1]) for x in data), best, aim)


def detect_build(equipment: dict, book: dict, potion_key: str = "", food_key: str = "") -> list[SlotSpell]:
    """Умения надетых предметов по клавишам (оружие Q/W/E, броня R, шлем D, обувь F, зелье, еда)."""
    items = (book or {}).get("items") or {}
    spells = (book or {}).get("spells") or {}
    out: list[SlotSpell] = []
    for slot, keys in SLOT_KEYS.items():
        entry = items.get(base_id(equipment.get(slot, "")))
        if not entry:
            continue
        for key, part in keys:
            names = [n for n in entry.get(part) or [] if n in spells]
            if names:
                out.append(slot_spell(key, slot, equipment[slot], names, spells))
    for slot, key, kind in (("зелье", potion_key, None), ("еда", food_key, "food")):
        entry = items.get(base_id(equipment.get(slot, "")))
        names = [n for n in (entry or {}).get("a") or [] if n in spells]
        if key and names:
            out.append(slot_spell(key.lower(), slot, equipment[slot], names, spells, kind))
    return out


def auto_skills(build: list[SlotSpell], manual: list[Skill] | None = None,
                learned: dict | None = None) -> list[Skill]:
    """Порядок и условия умений из билда. Ручной список («q:3@boss …») важнее для своих клавиш.

    Лечение — при здоровье ниже 60 %, щит и усиления брони/шлема — ниже 75 %, рывки
    и еда в бою не нажимаются (зелье жмёт отдельная проверка здоровья)."""
    manual = manual or []
    learned = learned or {}
    own = {s.key for s in manual}
    ranked = []
    for b in build:
        if b.key in own or b.kind in ("move", "food") or b.slot == "зелье":
            continue
        cd = float(learned.get(b.key) or b.cd)
        opts: dict = {"cast": b.cast, "aim": b.aim, "kind": b.kind}
        if b.kind == "heal":
            opts.update(hp_below=60.0, self_cast=True, aim=False)
        elif b.kind in ("shield", "buff") and b.slot in ("броня", "голова"):
            opts.update(hp_below=75.0, self_cast=True, aim=False)
        elif b.kind in ("shield", "buff"):
            opts.update(self_cast=True, aim=False)
        # Порядок: лечение и защита, контроль, потом урон — сначала долгие умения (E раньше Q).
        ranked.append((KIND_ORDER.index(b.kind), -cd, Skill(b.key, max(0.2, cd), **opts)))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [*manual, *(s for *_, s in ranked)]


class KeyLearner:
    """Перезарядка клавиши по факту: успешное нажатие (игра отправила запрос) даёт
    верхнюю границу, неуспешное (умение ещё не готово) — нижнюю; следующая попытка —
    посередине, пока границы не сойдутся."""

    TIGHT = 1.5          # точнее не нужно: бой проверяет клавиши примерно раз в секунду

    def __init__(self, prior: float, cd: float | None = None, lo: float = 0.0):
        self.prior = max(0.2, float(prior or DEFAULT_CD))
        self.hi: float | None = cd
        self.lo = float(lo or 0.0)
        self.last_ok: float | None = None
        self.fails = 0
        self.never_ok = 0          # неудачи, пока клавиша ни разу не сработала
        self.ready_at = 0.0
        self.inactive_until = 0.0

    @property
    def learned(self) -> float | None:
        """Перезарядка, если границы сошлись."""
        if self.hi is not None and self.hi - self.lo <= self.TIGHT:
            return round(self.hi, 1)
        return None

    def estimate(self) -> float:
        return self.hi if self.hi is not None else self.prior

    def _gap_after_ok(self) -> float:
        if self.hi is None:
            return self.prior
        if self.hi - self.lo <= self.TIGHT:
            return self.hi
        return max(self.lo + 0.3, (self.lo + self.hi) / 2)

    def ok(self, now: float) -> None:
        if self.last_ok is not None:
            e = now - self.last_ok
            self.hi = e if self.hi is None else min(self.hi, e)
            if e < self.lo:                    # прежняя «неготовность» была не из-за перезарядки
                self.lo = max(0.0, e - 0.3)
        self.last_ok = now
        self.fails = self.never_ok = 0
        self.ready_at = now + self._gap_after_ok()

    def fail(self, now: float) -> None:
        self.fails += 1
        if self.last_ok is None:
            self.never_ok += 1
            if self.never_ok >= 4:             # клавиша пустая или умение пассивное
                self.never_ok = 0
                self.inactive_until = now + 60
            self.ready_at = max(now + 3.0, self.inactive_until)
            return
        e = now - self.last_ok
        if e < self.estimate() * 3 + 10:
            self.lo = max(self.lo, e)          # ещё перезаряжается
            if self.hi is not None and self.lo >= self.hi:
                self.hi = None                 # перезарядка выросла (сменили умение)
        elif self.fails >= 6:
            self.inactive_until = now + 60     # нет энергии, немота — отдохнуть от клавиши
        target = self.hi if self.hi is not None and self.hi > e else max(e * 1.25, e + 0.5)
        self.ready_at = max(now + min(5.0, max(0.3, target - e)), self.inactive_until)

    def state(self) -> dict:
        return {"cd": self.hi, "lo": round(self.lo, 2)}
