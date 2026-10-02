"""Оценка угрозы игроков рядом с ботом (по радару его окна).

Уровни: 0 — не угроза (друзья из списка, игроки далеко), 1 — осторожно (держаться
подальше), 2 — опасно (уходить сразу: быстрый выход из данжа, зелье, маунт).
Опасность растёт за враждебный флаг, сильное снаряжение, группу рядом с игроком и
за то, что игрок к нам приближается.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .radar_data import player_power

CAUTION, DANGER = 1, 2


@dataclass
class Threat:
    entity: object
    level: int
    dist: float
    reasons: list

    @property
    def name(self) -> str:
        return getattr(self.entity, "name", "") or "игрок"

    def text(self) -> str:
        return f"{self.name} ({', '.join(self.reasons) or 'рядом'}, {self.dist:.0f} м)"


def friends_of(d: dict) -> set[str]:
    return {n.strip().lower() for n in str(d.get("friends") or "").replace(";", ",").replace("\n", ",").split(",")
            if n.strip()}


class ThreatTracker:
    """Помнит прошлые расстояния до игроков, чтобы видеть, кто приближается."""

    def __init__(self):
        self.last: dict[int, float] = {}

    def assess(self, feed, pos: tuple[float, float], d: dict) -> list[Threat]:
        radius = float(d.get("player_radius") or 45)
        friends = friends_of(d)
        danger_ip = float(d.get("danger_ip") or 0)
        players = feed.entities("player")
        out = []
        seen = {}
        for e in players:
            dist = math.hypot(e.x - pos[0], e.y - pos[1])
            seen[e.id] = dist
            if dist > radius or (e.name or "").lower() in friends:
                continue
            score, reasons = 1, []
            if e.faction == 255:
                score += 2
                reasons.append("враждебный")
            prev = self.last.get(e.id)
            if prev is not None and prev - dist > 3:
                score += 1
                reasons.append("приближается")
            if danger_ip:
                radar = feed.radar
                with radar.lock:
                    items = radar._items(e.equipment)
                ip = player_power(items, radar.item_ip).get("ip")
                if ip and ip >= danger_ip:
                    score += 1
                    reasons.append(f"сила {ip:.0f}")
            group = sum(1 for o in players if o.id != e.id and math.hypot(o.x - e.x, o.y - e.y) < 15
                        and (o.name or "").lower() not in friends)
            if group:
                score += min(2, group)
                reasons.append(f"группа {group + 1}")
            out.append(Threat(e, DANGER if score >= 3 else CAUTION, dist, reasons))
        self.last = seen
        out.sort(key=lambda t: (-t.level, t.dist))
        return out
