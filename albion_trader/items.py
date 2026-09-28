"""Названия предметов.

Файл с названиями скачивается один раз командой ``update-items`` из открытого
репозитория ao-data/ao-bin-dumps (это только загрузка справочника — никакие
ваши данные при этом не передаются). Без него приложение показывает
идентификаторы предметов.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"

_TIER_RE = re.compile(r"^T(\d)_")


class ItemCatalog:
    def __init__(self, names: dict | None = None, index: dict | None = None):
        self.names: dict[str, dict] = names or {}
        self.index: dict[str, str] = index or {}  # AlbionId (числовой индекс) -> UniqueName

    @classmethod
    def load(cls, path: str | Path) -> "ItemCatalog":
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data.get("names"), data.get("index"))

    def __len__(self) -> int:
        return len(self.names)

    def name(self, item_id: str, lang: str = "ru") -> str:
        entry = self.names.get(item_id)
        if entry:
            return entry.get(lang) or entry.get("en") or item_id
        return item_id

    def search_text(self, item_id: str) -> str:
        entry = self.names.get(item_id) or {}
        return " ".join([item_id, entry.get("ru", ""), entry.get("en", "")]).lower()


def tier_of(item_id: str) -> int | None:
    m = _TIER_RE.match(item_id)
    return int(m.group(1)) if m else None


def enchant_of(item_id: str) -> int:
    if "@" in item_id:
        try:
            return int(item_id.rsplit("@", 1)[1])
        except ValueError:
            return 0
    return 0


def build_catalog(raw_items: list) -> dict:
    names, index = {}, {}
    for it in raw_items:
        uid = it.get("UniqueName")
        if not uid:
            continue
        loc = it.get("LocalizedNames") or {}
        names[uid] = {"en": loc.get("EN-US") or uid, "ru": loc.get("RU-RU") or loc.get("EN-US") or uid}
        if it.get("Index") is not None:
            index[str(it["Index"])] = uid
    return {"names": names, "index": index}


def download_catalog(path: str | Path, url: str = ITEMS_URL) -> int:
    with urllib.request.urlopen(url, timeout=120) as resp:
        raw = json.load(resp)
    catalog = build_catalog(raw)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return len(catalog["names"])
