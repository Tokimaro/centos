"""Доска судьбы: названия узлов, слава по уровням, прогресс и прогноз."""

from __future__ import annotations

import re

from .gamedata import GameData

CATEGORY_NAMES = {"fighting": "Бой", "crafting": "Ремесло", "gathering": "Сбор", "farming": "Фермерство",
                  "tracking": "Выслеживание"}
_TIER_SUFFIX = re.compile(r"\s*\((?:[^()]*)\)\s*$")
_TIER_PREFIX_EN = re.compile(r"^(?:Beginner|Novice|Journeyman|Adept|Expert|Master|Grandmaster|Elder)'s\s+")


def node_title(gd: GameData, name_of, node_id: str) -> str:
    """Название узла: предмет-иконка без пометки тира; у общих веток — «(ветка)»."""
    node = gd.destiny.get("nodes", {}).get(node_id)
    if not node:
        return node_id
    item = node.get("item") or ""
    name = name_of(item) if item else ""
    if not name or name == item:
        name = node_id.split("_", 1)[-1].replace("_", " ").title()
    name = _TIER_PREFIX_EN.sub("", _TIER_SUFFIX.sub("", name))
    if node.get("base"):
        name += " (ветка)"
    return f"{CATEGORY_NAMES.get(node.get('cat'), node.get('cat') or '')}: {name}".strip(": ")
