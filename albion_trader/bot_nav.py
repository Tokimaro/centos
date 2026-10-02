"""Навигация ботов: маршрут между зонами и путь внутри зоны в обход препятствий.

* **Между зонами.** Список зон (``cluster/world.json``) знает выходы каждой зоны и
  куда они ведут. Маршрут — кратчайший по длине пути (от входа в зону до нужного
  выхода) с ограничением по опасности зон: только безопасные, с жёлтыми, с красными
  или любые. Данжи, туннели Авалона, острова и т. п. в маршрут не попадают.
* **Внутри зоны.** Схема зоны (те же тайлы, что у фона радара) превращается в сетку
  проходимости: вода, скалы, постройки, камни и деревья — препятствия, дороги и
  мостки — проходимы (мосты над водой). По сетке ищется путь A*, затем лишние точки
  выкидываются (прямая видимость). Схемы нет или путь не найден — идём напрямую.
"""

from __future__ import annotations

import heapq
import math

CELL = 3.0                     # шаг сетки проходимости, м
MAX_EXPANSIONS = 250_000
BLOCKING = {"water", "cliff", "building", "rock", "tree"}
PASSABLE_OVER = {"road", "path"}   # дороги и мостки поверх воды и скал

# Типы зон (cluster @type).
NEVER = ("DUNGEON", "TUNNEL", "HIDEOUT", "ISLAND", "HALL_OF_FAME", "ARENA", "EXPEDITION", "MISTS", "CORRUPTED")
SAFETY = {"safe": 0, "yellow": 1, "red": 2, "black": 3}


def zone_danger(ztype: str) -> int:
    """0 — безопасная, 1 — жёлтая, 2 — красная, 3 — чёрная."""
    t = (ztype or "").upper()
    if "BLACK" in t:
        return 3
    if "RED" in t:
        return 2
    if "YELLOW" in t:
        return 1
    return 0


def zone_allowed(ztype: str, safety: str) -> bool:
    t = (ztype or "").upper()
    if any(w in t for w in NEVER):
        return False
    return zone_danger(t) <= SAFETY.get(safety, 0)


def _rotate(x: float, z: float, deg: float) -> tuple[float, float]:
    """Тот же поворот, что у схем зон (Unity, вокруг вертикали)."""
    if not deg:
        return x, z
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + z * s, -x * s + z * c


# --- маршрут между зонами ------------------------------------------------------
class Router:
    def __init__(self, index: dict):
        self.index = index or {}

    def name(self, cid: str) -> str:
        return (self.index.get(cid) or {}).get("name") or cid

    def exits(self, cid: str) -> list:
        return [(float(x), float(y), t) for x, y, t, *_ in (self.index.get(cid) or {}).get("exits") or []]

    def nearby(self, start: str, safety: str = "yellow", limit: int = 4) -> list[str]:
        """Ближайшие к ``start`` зоны открытого мира (не города), в порядке удаления по переходам."""
        seen, order, queue = {start}, [], [start]
        while queue and len(order) < limit:
            nxt = []
            for zone in queue:
                for _x, _y, target in self.exits(zone):
                    if target in seen or target not in self.index:
                        continue
                    seen.add(target)
                    ztype = self.index[target].get("type", "")
                    if not zone_allowed(ztype, safety):
                        continue
                    nxt.append(target)
                    if "PLAYERCITY" not in ztype.upper():
                        order.append(target)
            queue = nxt
        return order[:limit]

    def route(self, start: str, goal: str, safety: str = "safe",
              start_pos: tuple[float, float] | None = None) -> list[tuple[str, float, float, str]]:
        """Список переходов [(зона, x выхода, y выхода, следующая зона)] от ``start`` до ``goal``.

        Пустой список — уже на месте. Нет пути — ValueError с понятной причиной."""
        if start == goal:
            return []
        if start not in self.index:
            raise ValueError(f"зоны «{start}» нет в списке зон — обновите карты (update-maps)")
        if goal not in self.index:
            raise ValueError(f"зоны «{goal}» нет в списке зон")
        # Узел — (зона, точка входа); стоимость — пройденные метры плюс штраф за переход.
        best: dict[tuple, float] = {}
        prev: dict[tuple, tuple] = {}
        sx, sy = start_pos if start_pos else (0.0, 0.0)
        first = (start, round(sx, 1), round(sy, 1))
        heap = [(0.0, first)]
        best[first] = 0.0
        end = None
        while heap:
            cost, node = heapq.heappop(heap)
            if cost > best.get(node, 1e18):
                continue
            zone, ex, ey = node
            if zone == goal:
                end = node
                break
            for x, y, target in self.exits(zone):
                if target not in self.index:
                    continue
                if target != goal and not zone_allowed(self.index[target].get("type", ""), safety):
                    continue
                walk = 0.0 if node is first and start_pos is None else math.hypot(x - ex, y - ey)
                back = [(bx, by) for bx, by, t in self.exits(target) if t == zone]
                bx, by = min(back, key=lambda p: math.hypot(p[0] - x, p[1] - y)) if back else (x, y)
                nxt = (target, round(bx, 1), round(by, 1))
                c = cost + walk + 60.0
                if c < best.get(nxt, 1e18):
                    best[nxt] = c
                    prev[nxt] = (node, x, y)
                    heapq.heappush(heap, (c, nxt))
        if end is None:
            raise ValueError(f"нет пути из «{self.name(start)}» в «{self.name(goal)}» по зонам с допустимой "
                             f"опасностью ({safety}) — разрешите более опасные зоны")
        hops = []
        node = end
        while node in prev:
            p, x, y = prev[node]
            hops.append((p[0], x, y, node[0]))
            node = p
        return hops[::-1]


# --- путь внутри зоны ------------------------------------------------------------
class Grid:
    """Сетка проходимости зоны по тайлам схемы."""

    def __init__(self, zonemap: dict, cell: float = CELL):
        self.cell = cell
        tiles = zonemap.get("tiles") or []
        b = list(zonemap.get("bounds") or [0, 0, 0, 0])
        if not any(b) and tiles:
            xs = [t[1] for t in tiles]
            ys = [t[2] for t in tiles]
            b = [min(xs) - 20, min(ys) - 20, max(xs) + 20, max(ys) + 20]
        self.x0, self.y0 = min(b[0], b[2]), min(b[1], b[3])
        self.w = max(1, int(math.ceil((max(b[0], b[2]) - self.x0) / cell)))
        self.h = max(1, int(math.ceil((max(b[1], b[3]) - self.y0) / cell)))
        self.blocked = bytearray(self.w * self.h)
        for t in tiles:
            if t[0] in BLOCKING:
                self._paint(t, 1)
        for t in tiles:
            if t[0] in PASSABLE_OVER:
                self._paint(t, 0)
        # Клетки рядом с препятствием: путь старается держаться от них подальше,
        # иначе шаг с небольшим разбросом цепляет угол стены.
        self.near = bytearray(self.w * self.h)
        for cy in range(self.h):
            for cx in range(self.w):
                if self.blocked[cy * self.w + cx]:
                    for ny in range(max(0, cy - 1), min(self.h, cy + 2)):
                        for nx in range(max(0, cx - 1), min(self.w, cx + 2)):
                            self.near[ny * self.w + nx] = 1

    def _paint(self, t, value: int) -> None:
        _cat, x, y, w, h, rot = t[:6]
        r = math.hypot(w, h) / 2
        c0, r0 = self.cell_of(x - r, y - r)
        c1, r1 = self.cell_of(x + r, y + r)
        for cy in range(max(0, r0), min(self.h - 1, r1) + 1):
            for cx in range(max(0, c0), min(self.w - 1, c1) + 1):
                px, py = self.center(cx, cy)
                lx, ly = _rotate(px - x, py - y, -rot)
                if abs(lx) <= w / 2 and abs(ly) <= h / 2:
                    self.blocked[cy * self.w + cx] = value

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        return int((x - self.x0) // self.cell), int((y - self.y0) // self.cell)

    def center(self, cx: int, cy: int) -> tuple[float, float]:
        return self.x0 + (cx + 0.5) * self.cell, self.y0 + (cy + 0.5) * self.cell

    def inside(self, cx: int, cy: int) -> bool:
        return 0 <= cx < self.w and 0 <= cy < self.h

    def free(self, cx: int, cy: int) -> bool:
        return self.inside(cx, cy) and not self.blocked[cy * self.w + cx]

    def free_at(self, x: float, y: float) -> bool:
        return self.free(*self.cell_of(x, y))

    def nearest_free(self, cx: int, cy: int, radius: int = 8) -> tuple[int, int] | None:
        if self.free(cx, cy):
            return cx, cy
        for r in range(1, radius + 1):
            ring = [(cx + dx, cy + dy) for dx in range(-r, r + 1) for dy in (-r, r)] + \
                   [(cx + dx, cy + dy) for dx in (-r, r) for dy in range(-r + 1, r)]
            ring = [c for c in ring if self.free(*c)]
            if ring:
                return min(ring, key=lambda c: (c[0] - cx) ** 2 + (c[1] - cy) ** 2)
        return None

    def is_near(self, cx: int, cy: int) -> bool:
        return self.inside(cx, cy) and bool(self.near[cy * self.w + cx])

    def line_free(self, a: tuple[int, int], b: tuple[int, int], clear: bool = False) -> bool:
        """Прямая видимость между клетками; ``clear`` — ещё и не вплотную к препятствиям
        (кроме концов отрезка)."""
        (x0, y0), (x1, y1) = a, b
        n = max(abs(x1 - x0), abs(y1 - y0))
        for i in range(n + 1):
            t = i / n if n else 0
            c = (round(x0 + (x1 - x0) * t), round(y0 + (y1 - y0) * t))
            if not self.free(*c) or (clear and 0 < i < n and self.is_near(*c)):
                return False
        return True

    def path(self, start: tuple[float, float], goal: tuple[float, float]) -> list[tuple[float, float]] | None:
        """Точки пути от ``start`` до ``goal`` (без старта, последняя — сама цель) или None."""
        s = self.nearest_free(*self.cell_of(*start))
        g = self.nearest_free(*self.cell_of(*goal))
        if s is None or g is None:
            return None
        if s == g:
            return [goal]
        dirs = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]
        openq = [(0.0, s)]
        cost = {s: 0.0}
        came: dict = {}
        expanded = 0
        while openq:
            _f, cur = heapq.heappop(openq)
            if cur == g:
                break
            expanded += 1
            if expanded > MAX_EXPANSIONS:
                return None
            for dx, dy, step in dirs:
                nx, ny = cur[0] + dx, cur[1] + dy
                if not self.free(nx, ny):
                    continue
                if dx and dy and not (self.free(cur[0] + dx, cur[1]) and self.free(cur[0], cur[1] + dy)):
                    continue    # не срезаем угол препятствия
                c = cost[cur] + step + (2.0 if self.near[ny * self.w + nx] else 0.0)
                if c < cost.get((nx, ny), 1e18):
                    cost[(nx, ny)] = c
                    came[(nx, ny)] = cur
                    heapq.heappush(openq, (c + math.hypot(g[0] - nx, g[1] - ny), (nx, ny)))
        if g not in came:
            return None
        cells = [g]
        while cells[-1] != s:
            cells.append(came[cells[-1]])
        cells.reverse()
        # Оставляем только повороты: следующая точка — самая дальняя в прямой видимости.
        keep, i = [], 0
        while i < len(cells) - 1:
            j = len(cells) - 1
            while j > i + 1 and not self.line_free(cells[i], cells[j], clear=True):
                j -= 1
            keep.append(cells[j])
            i = j
        pts = [self.center(*c) for c in keep]
        pts[-1] = goal
        return pts
