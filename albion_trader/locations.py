"""Справочник рыночных локаций.

Клиент присылает сырые идентификаторы локаций (``"3005"``, ``"3013-Auction2"``,
``"BLACKBANK-2310"`` …). Здесь они приводятся к стабильным ключам рынков,
чтобы у Карлеона, например, был один ключ независимо от того, какой из двух
идентификаторов рынка пришёл.
"""

from __future__ import annotations

from dataclasses import dataclass

BLACK_MARKET = "black_market"


@dataclass(frozen=True)
class Market:
    key: str
    name_ru: str
    name_en: str
    kind: str  # "city" | "black_market" | "portal" | "other"


_MARKETS = [
    Market("thetford", "Тетфорд", "Thetford", "city"),
    Market("lymhurst", "Лимхерст", "Lymhurst", "city"),
    Market("bridgewatch", "Бриджвотч", "Bridgewatch", "city"),
    Market("martlock", "Мартлок", "Martlock", "city"),
    Market("fort_sterling", "Форт Стерлинг", "Fort Sterling", "city"),
    Market("caerleon", "Карлеон", "Caerleon", "city"),
    Market("brecilien", "Брецилиэн", "Brecilien", "city"),
    Market(BLACK_MARKET, "Чёрный рынок", "Black Market", "black_market"),
]

MARKETS: dict[str, Market] = {m.key: m for m in _MARKETS}

# Сырые ID локаций (см. ao-bin-dumps/formatted/world.txt) -> ключ рынка.
# Как в AODP (albiondata-deduper): 3003 — Чёрный рынок, 3005 и 3013 — Карлеон,
# рынки порталов (0301, 1301, …) — это рынки соответствующих городов, у них
# общий стакан.
_RAW_TO_KEY = {
    "0007": "thetford",
    "1002": "lymhurst",
    "2004": "bridgewatch",
    "3008": "martlock",
    "4002": "fort_sterling",
    "3005": "caerleon",
    "3013": "caerleon",
    "3013-Auction2": "caerleon",
    "5003": "brecilien",
    "3003": BLACK_MARKET,
    "0301": "thetford",
    "1301": "lymhurst",
    "2301": "bridgewatch",
    "3301": "martlock",
    "4301": "fort_sterling",
}

# Ключи рынков порталов из версии 0.2 — переносятся в города при запуске.
LEGACY_KEYS = {
    "thetford_portal": "thetford",
    "lymhurst_portal": "lymhurst",
    "bridgewatch_portal": "bridgewatch",
    "martlock_portal": "martlock",
    "fort_sterling_portal": "fort_sterling",
}

DEFAULT_CITIES = [m.key for m in _MARKETS if m.kind == "city"]


def normalize_location(raw) -> str | None:
    """Возвращает ключ рынка для сырого LocationId или None, если он пустой."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        # Старые версии клиента присылали числа (7 вместо "0007").
        s = s.zfill(4)
    return _RAW_TO_KEY.get(s, s)


def market_info(key: str) -> dict:
    m = MARKETS.get(key)
    if m:
        return {"key": m.key, "name": m.name_ru, "name_en": m.name_en, "kind": m.kind}
    if key.startswith("BLACKBANK-"):
        return {
            "key": key,
            "name": f"Логово контрабандистов {key[len('BLACKBANK-'):]}",
            "name_en": f"Smuggler's Den {key[len('BLACKBANK-'):]}",
            "kind": "other",
        }
    return {"key": key, "name": key, "name_en": key, "kind": "other"}
