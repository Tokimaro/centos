"""Закодированные позиции игроков.

Игра (по крайней мере на части серверов) передаёт координаты игроков не числами, а
8 байтами: x и y (float32) с наложенным XOR-ключом. Ключ общий для всех игроков и
меняется каждые 10 секунд игрового времени (метка времени .NET в событии движения),
а сам ключ сервер не присылает. Мобы, ресурсы и своя позиция — открытые.

Ключ подбирается на лету: старшие два байта каждого числа (знак, порядок, старшие биты
мантиссы) выбираются так, чтобы все игроки интервала оказались недалеко от своей
позиции и двигались плавно (не быстрее своей скорости из того же события — она
открытая). Младшие байты ключа не нужны: их ошибка — доли метра.

Проверено на записи трафика: скорость по раскодированным координатам совпадает
с переданной скоростью (медиана отношения ≈ 1,00).
"""

from __future__ import annotations

import math
import struct

PERIOD = 100_000_000        # 10 с в тиках .NET (100 нс)
OFFSET = 20_000_000         # границы интервалов сдвинуты на ~2 с
EDGE = 1_500_000            # ±0,15 с у границы — ключ мог быть ещё прежним
VIEW = 90.0                 # игроки видны примерно в этом радиусе, м
MAX_SAMPLES = 400           # хранить на интервал
SOLVE_SAMPLES = 80          # брать в подбор
MIN_SAMPLES = 12            # меньше — ключ не подбирать (легко ошибиться)
CONFIDENT = 20              # ключ по стольким образцам — опора для следующего интервала
PRIOR = 0.02                # при прочих равных — игроки ближе ко мне
KEEP_WINDOWS = 6            # образцы — за столько интервалов
KEEP_KEYS = 60              # ключи — 10 минут: стоящий игрок раскодируется и позже
PRIOR_SINGLE = 0.15         # стоящий (одна позиция, без опоры) — сильнее «рядом со мной»


def window_of(ticks: int) -> int:
    return (ticks - OFFSET) // PERIOD


def _dec(b: bytes, k2: int, k3: int, k1: int = 0) -> float:
    return struct.unpack("<f", bytes((b[0], b[1] ^ k1, b[2] ^ k2, b[3] ^ k3)))[0]


def _pairs(rows: list, axis: int) -> list:
    by: dict = {}
    for _ticks, oid, enc, _speed in rows:
        by.setdefault(oid, []).append(enc[axis * 4:axis * 4 + 4])
    return [(a, b) for lst in by.values() for a, b in zip(lst, lst[1:]) if a != b]


def _tv(pairs: list, k2: int, k3: int, k1: int, limit: float = math.inf) -> float:
    tv = 0.0
    for a, b in pairs:
        tv += abs(_dec(b, k2, k3, k1) - _dec(a, k2, k3, k1))
        if tv >= limit:
            break
    return tv


def _refine_low(rows: list, axis: int, k2: int, k3: int) -> tuple[int, int]:
    """Третий байт ключа — по плавности путей (с неверным путь «дрожит» до метра)."""
    pairs = _pairs(rows, axis)
    if len(pairs) < 5:
        return k2, 0
    best = (math.inf, 0)
    for k1 in range(256):
        tv = _tv(pairs, k2, k3, k1, best[0])
        if tv < best[0]:
            best = (tv, k1)
    return k2, best[1]


def move_parts(block) -> tuple[int, bytes, float] | None:
    """Событие движения (байтовый блок): метка времени, 8 байт позиции, скорость."""
    if not isinstance(block, (bytes, bytearray)) or len(block) < 17:
        return None
    ticks = struct.unpack_from("<q", block, 1)[0]
    speed = struct.unpack_from("<f", block, 18)[0] if len(block) >= 22 else 0.0
    if not math.isfinite(speed) or not 0 <= speed < 50:
        speed = 0.0
    return ticks, bytes(block[9:17]), speed


def _subsample(rows: list) -> list:
    """Для подбора: первая позиция каждого игрока (стоящие — только она) и равномерно
    остальные, всего не больше SOLVE_SAMPLES + число игроков."""
    first: dict = {}
    for r in rows:
        first.setdefault(r[1], r)
    rest = [r for r in rows if first.get(r[1]) is not r]
    step = max(1, len(rest) // SOLVE_SAMPLES)
    return sorted([*first.values(), *rest[::step]])[:SOLVE_SAMPLES + 120]


class PositionDecoder:
    """Подбор ключа по интервалам и раскодирование позиций игроков."""

    def __init__(self, center=lambda: (0.0, 0.0)):
        self.center = center                 # своя позиция (открытая) — где искать игроков
        self.samples: dict[int, list] = {}   # интервал → [(тики, id, 8 байт, скорость)]
        self.keys: dict[int, tuple] = {}     # интервал → ((k2x, k3x), (k2y, k3y), образцов)
        self.last: dict[int, tuple] = {}     # id → (тики, x, y) — последняя раскодированная позиция
        self.win_last: dict[int, dict] = {}  # интервал → {id: (тики, x, y)} — последние позиции в нём
        self.ticks = 0                       # последняя известная метка времени игры
        self.version = 0                     # растёт при каждом новом подборе ключа
        self.checked: set = set()            # интервалы, перепроверенные после окончания
        self.per_player: dict[int, dict] = {}  # интервал → {id: образцов}
        self.chained: dict[int, bool] = {}     # интервал подобран с опорой на прошлый

    # --- данные --------------------------------------------------------
    def observe(self, ticks: int) -> None:
        if ticks > self.ticks:
            self.ticks = ticks

    def add(self, oid: int, ticks: int, enc: bytes, speed: float = 0.0) -> None:
        self.observe(ticks)
        w = window_of(ticks)
        lst = self.samples.setdefault(w, [])
        per = self.per_player.setdefault(w, {})
        # Первые образцы каждого игрока — всегда (стоящих не вытесняет толпа идущих).
        if len(lst) < MAX_SAMPLES or (per.get(oid, 0) < 3 and len(lst) < MAX_SAMPLES * 2):
            lst.append((ticks, oid, enc, speed))
            per[oid] = per.get(oid, 0) + 1
        for old in [k for k in self.samples if k < w - KEEP_WINDOWS]:
            self.samples.pop(old, None)
            self.per_player.pop(old, None)
        for old in [k for k in self.keys if k < w - KEEP_KEYS]:
            self.keys.pop(old, None)
            self.win_last.pop(old, None)
            self.checked.discard(old)
            self.chained.pop(old, None)

    # --- ключ -----------------------------------------------------------
    @staticmethod
    def _cost(rows: list, axis: int, k2: int, k3: int, center: float, prev: dict,
              limit: float = math.inf, prior: float = PRIOR, single: frozenset = frozenset()) -> float:
        """Насколько правдоподобен ключ: игроки рядом со мной (``center``), пути без
        скачков (не быстрее своей скорости), продолжение путей прошлого интервала (``prev``)."""
        cost = 0.0
        last: dict = {}
        for ticks, oid, enc, speed in rows:
            v = _dec(enc[axis * 4:axis * 4 + 4], k2, k3)
            if not math.isfinite(v) or abs(v - center) > VIEW * 1.5:
                cost += 50
                continue
            if abs(v - center) > VIEW:
                cost += 5
            cost += (PRIOR_SINGLE if oid in single and prior else prior) * abs(v - center)
            ref = last.get(oid) or prev.get(oid)
            if ref is not None:
                dt = abs(ticks - ref[0]) / 1e7
                if dt < 3:
                    cost += max(0.0, abs(v - ref[1]) - (max(speed, 6.0) * dt + 1.0))
            last[oid] = (ticks, v)
            if cost >= limit:
                break
        return cost

    def _solve_axis(self, rows: list, axis: int, center: float, prev: dict) -> tuple[float, int, int]:
        tops = {struct.pack("<f", center + d)[3] for d in (-VIEW, -VIEW / 3, 0.0, VIEW / 3, VIEW)}
        votes: dict[int, int] = {}
        for r in rows:
            b3 = r[2][axis * 4 + 3]
            for t in tops:
                for tt in (t, t ^ 1):
                    votes[b3 ^ tt] = votes.get(b3 ^ tt, 0) + 1
        counts: dict = {}
        for r in rows:
            counts[r[1]] = counts.get(r[1], 0) + 1
        single = frozenset(oid for oid, n in counts.items() if n == 1 and oid not in prev)
        best = (math.inf, 0, 0)
        for k3 in sorted(votes, key=lambda k: -votes[k])[:4]:
            for k2 in range(256):
                cost = self._cost(rows, axis, k2, k3, center, prev, best[0], single=single)
                if cost < best[0]:
                    best = (cost, k2, k3)
        return best

    def key(self, w: int):
        rows = self.samples.get(w) or []
        known = self.keys.get(w)
        finished = window_of(self.ticks) > w
        if known and finished and w not in self.checked:
            self.checked.add(w)
            return self._recheck(w, known)
        if not rows or (known and len(rows) < known[2] * 1.5 + 3):
            return known
        if len(rows) < MIN_SAMPLES and not (finished and len(rows) >= 3):
            return known
        # Образцы у границы интервала не берём: ключ там мог быть ещё прежним.
        inner = [r for r in rows if EDGE < (r[0] - OFFSET) % PERIOD < PERIOD - EDGE] or rows
        sample = _subsample(inner)
        cx, cy = self.center()
        # Опора — позиции из уверенно разобранного прошлого интервала.
        pk = self.keys.get(w - 1)
        trusted = self.win_last.get(w - 1, {}) if pk and pk[2] >= CONFIDENT else {}
        prev_x = {oid: (t, x) for oid, (t, x, _y) in trusted.items()}
        prev_y = {oid: (t, y) for oid, (t, _x, y) in trusted.items()}
        _cx, k2x, k3x = self._solve_axis(sample, 0, cx, prev_x)
        _cy, k2y, k3y = self._solve_axis(sample, 1, cy, prev_y)
        srt = sorted(inner)[:SOLVE_SAMPLES * 3]
        k2x, k1x = _refine_low(srt, 0, k2x, k3x)
        k2y, k1y = _refine_low(srt, 1, k2y, k3y)
        new = ((k2x, k3x, k1x), (k2y, k3y, k1y), len(rows))
        if not known or new[:2] != known[:2]:
            self.version += 1
        self.keys[w] = new
        self.chained[w] = bool(trusted)
        return new

    def _recheck(self, w: int, known: tuple):
        """Интервал закончился: проверить ключ по всем образцам без опоры на прошлый
        интервал — если опора была ошибочной, путь внутри интервала покажет скачки."""
        rows = sorted(r for r in self.samples.get(w) or [] if EDGE < (r[0] - OFFSET) % PERIOD < PERIOD - EDGE)
        if len(rows) < CONFIDENT:
            return known
        cx, cy = self.center()
        out = list(known)
        changed = False
        for axis, c in ((0, cx), (1, cy)):
            k2, k3, k1 = known[axis]
            cur = self._cost(rows, axis, k2, k3, c, {}, prior=0.0)
            alt_cost, a2, a3 = self._solve_axis(_subsample(rows), axis, c, {})
            alt = self._cost(rows, axis, a2, a3, c, {}, prior=0.0)
            if (a2, a3) != (k2, k3) and alt + 5 < cur:
                a2, a1 = _refine_low(rows, axis, a2, a3)
                out[axis] = (a2, a3, a1)
                changed = True
        if changed:
            self.keys[w] = (out[0], out[1], known[2])
            self.version += 1
            # Позиции конца интервала — заново (опора для следующего).
            last = {}
            (k2x, k3x, k1x), (k2y, k3y, k1y), _n = self.keys[w]
            for ticks, oid, enc, _speed in rows:
                last[oid] = (ticks, _dec(enc[0:4], k2x, k3x, k1x), _dec(enc[4:8], k2y, k3y, k1y))
            self.win_last[w] = last
            nxt = self.keys.get(w + 1)
            if nxt is not None:                # следующий подобран по ошибочной опоре — заново
                self.keys[w + 1] = (nxt[0], nxt[1], 0)
                self.checked.discard(w + 1)
        return self.keys[w]

    # --- раскодирование -------------------------------------------------
    def decode(self, oid: int, enc: bytes, ticks: int | None = None) -> tuple[float, float] | None:
        """Позиция игрока или None (ключ ещё не подобран)."""
        ticks = ticks if ticks is not None else self.ticks
        if not ticks or not isinstance(enc, (bytes, bytearray)) or len(enc) < 8:
            return None
        w = window_of(ticks)
        cands = [w]
        phase = (ticks - OFFSET) % PERIOD
        if phase < EDGE:
            cands.append(w - 1)
        elif phase > PERIOD - EDGE:
            cands.append(w + 1)
        cx, cy = self.center()
        ref = self.last.get(oid)
        best = None
        for cw in cands:
            k = self.key(cw)
            if not k:
                continue
            (k2x, k3x, k1x), (k2y, k3y, k1y), _n = k
            x, y = _dec(enc[0:4], k2x, k3x, k1x), _dec(enc[4:8], k2y, k3y, k1y)
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            if ref is not None:
                score = math.hypot(x - ref[1], y - ref[2])
                dt = abs(ticks - ref[0]) / 1e7
                # Соседний ключ (у границы интервала) — только если путь продолжается.
                if cw != w and score > 12.0 * dt + 2.0:
                    continue
            else:
                if cw != w:
                    continue
                score = math.hypot(x - cx, y - cy)
            if best is None or score < best[0]:
                best = (score, x, y)
        if best is None or math.hypot(best[1] - cx, best[2] - cy) > VIEW * 2:
            return None
        self.last[oid] = (ticks, best[1], best[2])
        self.win_last.setdefault(window_of(ticks), {})[oid] = self.last[oid]
        if len(self.last) > 2000:
            self.last.pop(next(iter(self.last)))
        return best[1], best[2]
