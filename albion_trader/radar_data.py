"""Справочные данные и эвристики радара.

* Мобы: ``mobs.json`` из ao-bin-dumps → компактная таблица ``data/mobs.json``
  ``[[уникальное имя, тир, категория], …]`` по порядку файла. Номер моба в событии
  NewMob — индекс в этом списке со сдвигом, который отличается между версиями
  игры и серверами; сдвиг задаётся в настройках радара (по подсказке на карте
  видно, правильно ли он подобран).
* Сила и роль игрока по снаряжению.
* Распознавание событий по форме параметров — для своего сервера, где номера
  событий отличаются от официальных.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Callable

from .gamedata import DUMPS_URL

log = logging.getLogger("albion_trader.radar")

# --- мобы ------------------------------------------------------------------
_LIVING = [("HIDE", "hide"), ("FIBER", "fiber"), ("WOOD", "wood"), ("ORE", "ore"), ("ROCK", "rock")]
_CATEGORY_RU = {"boss": "босс", "champion": "чемпион", "miniboss": "мини-босс", "elite": "элита",
                "harmless": "мирный", "trash": "", "standard": "", "summon": "призыв", "vanity": "",
                "chest": "сундук"}


def compact_mobs(raw: dict) -> list:
    mobs = ((raw or {}).get("Mobs") or {}).get("Mob") or []
    if isinstance(mobs, dict):
        mobs = [mobs]
    out = []
    for m in mobs:
        tier = m.get("@tier")
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            tier = None
        out.append([m.get("@uniquename") or "", tier, m.get("@mobtypecategory") or ""])
    return out


def pretty_mob(uniquename: str) -> str:
    """T8_MOB_HIDE_STEPPE_MAMMOTH → «hide steppe mammoth»."""
    s = re.sub(r"^T\d+_", "", uniquename)
    s = re.sub(r"^(MOB_|DYNAMIC_)+", "", s)
    s = re.sub(r"_(DYNAMIC|OBJ|NOCLICK)\b", "", s)
    return s.replace("_", " ").lower().strip()


class MobTable:
    def __init__(self, path: str | Path, base_url: str = DUMPS_URL,
                 fetch: Callable[[str], bytes] | None = None):
        self.path = Path(path)
        self.base_url = base_url
        self.fetch = fetch or (lambda p: urllib.request.urlopen(self.base_url + p, timeout=300).read())
        self.rows: list | None = None
        self._loading = False
        self.lock = threading.Lock()

    def load(self) -> bool:
        if self.rows is None and self.path.exists():
            try:
                self.rows = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.rows = None
        return self.rows is not None

    def save(self, raw: dict) -> int:
        rows = compact_mobs(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
        self.rows = rows
        return len(rows)

    def download_async(self) -> None:
        with self.lock:
            if self._loading or self.load():
                return
            self._loading = True

        def work():
            try:
                self.save(json.loads(self.fetch("mobs.json")))
                log.info("Справочник мобов: %d", len(self.rows or []))
            except (OSError, ValueError) as e:
                log.warning("Не удалось скачать mobs.json: %s", e)
            finally:
                self._loading = False
        threading.Thread(target=work, daemon=True).start()

    def info(self, type_id: int | None, offset: int = 0) -> dict | None:
        if type_id is None or not self.load():
            return None
        i = type_id + offset
        if not 0 <= i < len(self.rows):
            return None
        uname, tier, cat = self.rows[i]
        res = next((r for word, r in _LIVING if f"_{word}_" in f"_{uname}_"), "")
        return {"id": uname, "name": pretty_mob(uname), "tier": tier, "category": cat,
                "category_ru": _CATEGORY_RU.get(cat, cat), "res": res,
                "boss": cat in ("boss", "miniboss", "champion")}


# --- сила и роль игрока ------------------------------------------------------
GEAR_SLOTS = ("оружие", "вторая рука", "голова", "броня", "обувь", "плащ")
_ROLES = [("хил", ("HOLYSTAFF", "NATURESTAFF")),
          ("поддержка", ("ARCANESTAFF", "ENIGMATIC", "_2H_DIVINESTAFF")),
          ("танк", ("_MACE", "_HAMMER", "_2H_RAM", "SHAPESHIFTER_SET1", "_ROCKMACE", "_DUALMACE"))]


def player_power(equipment: list, item_ip: Callable[[str], float | None]) -> dict:
    """Средняя сила предметов (без учёта качества и специализации) и роль по оружию."""
    ips = [ip for it in equipment if it["slot"] in GEAR_SLOTS for ip in [item_ip(it["id"])] if ip]
    weapon = next((it["id"] for it in equipment if it["slot"] == "оружие"), "")
    role = "урон" if weapon else ""
    up = weapon.upper()
    for name, words in _ROLES:
        if any(w in up for w in words):
            role = name
            break
    return {"ip": round(sum(ips) / len(ips)) if ips else None, "role": role}


# --- распознавание событий по форме -----------------------------------------
def _is_pos(v) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(c, float) for c in v)) or \
           (isinstance(v, (bytes, bytearray)) and 17 <= len(v) <= 40)


def guess_event(params: dict) -> str | None:
    """Какое событие радара похоже на эти параметры (или None)."""
    p = {k: v for k, v in params.items() if isinstance(k, int) and k < 250}
    if not isinstance(p.get(0), int) or isinstance(p.get(0), bool):
        ids = p.get(0)
        pos = p.get(3)
        if isinstance(ids, (list, tuple)) and ids and isinstance(pos, (list, tuple)) \
                and len(pos) == 2 * len(ids) and all(isinstance(c, float) for c in pos[:4]):
            return "new_simple_harvestable_object_list"
        return None
    n = len(p)
    if isinstance(p.get(1), (bytes, bytearray)) and 17 <= len(p[1]) <= 40 and n <= 6:
        return "move"
    if isinstance(p.get(1), str) and p[1] and n >= 15 and any(_is_pos(p.get(k)) for k in (12, 13, 14)):
        return "new_character"
    if isinstance(p.get(1), int) and not isinstance(p.get(1), bool) and n >= 8 and _is_pos(p.get(7)):
        return "new_mob"
    t = p.get(7)
    if _is_pos(p.get(8)) and isinstance(p.get(5), int) and isinstance(t, int) and 1 <= t <= 8 and n <= 16:
        return "new_harvestable_object"
    return None


class CodeGuesser:
    """Копит догадки по кодам событий и предлагает исправить номера."""

    MIN_SAMPLES = 5

    def __init__(self):
        self.votes: dict[int, Counter] = {}
        self.seen: Counter = Counter()

    def observe(self, code: int, params: dict) -> None:
        self.seen[code] += 1
        g = guess_event(params)
        if g:
            self.votes.setdefault(code, Counter())[g] += 1

    def suggestions(self, events: dict[str, int]) -> list[dict]:
        """[{name, code, current, samples, share}] — коды, которые стоит назначить событиям."""
        by_name: dict[str, tuple[int, int, float]] = {}
        for code, votes in self.votes.items():
            name, n = votes.most_common(1)[0]
            share = n / max(1, self.seen[code])
            if n < self.MIN_SAMPLES or share < 0.8:
                continue
            best = by_name.get(name)
            if not best or n > best[1]:
                by_name[name] = (code, n, share)
        out = []
        for name, (code, n, share) in sorted(by_name.items()):
            current = events.get(name)
            if current == code:
                continue
            # Настроенный код уже приходит с такой же формой — значит, он верен.
            cur_votes = self.votes.get(current, Counter()) if current is not None else Counter()
            if cur_votes.get(name, 0) >= self.MIN_SAMPLES:
                continue
            out.append({"name": name, "code": code, "current": current, "samples": n, "share": round(share, 2)})
        return out
