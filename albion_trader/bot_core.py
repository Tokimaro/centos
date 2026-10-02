"""Общее для бота: трафик окна игры, перевод экрана в мир, макросы и шаблоны действий.

* :class:`ClientFeed` — разбор трафика одного клиента игры (свой UDP-порт): позиция,
  зона, персонаж, здоровье, объекты вокруг (тот же радар).
* :class:`Calibration` — где персонаж на экране и как смещение на экране переходит в
  смещение в мире (камера игры повёрнута и наклонена).
* Макросы — строки действий (клик, ввод, клавиша, пауза, проверка запроса). Для рынка
  и добычи макросы собираются сами из **точек интерфейса**, которые пользователь
  один раз показывает кликом в окне игры (мастер точек).
"""

from __future__ import annotations

import collections
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .bot_win import parse_keys
from .capture.albion import AlbionState
from .capture.photon import PhotonParser

STEP = 0.26               # самый длинный шаг-клик, доля высоты окна
EDGE = 0.07               # не кликать ближе к краю окна (там интерфейс)


class BotStopped(Exception):
    """Бота остановили — выход из задачи."""


class BotError(Exception):
    """Задачу нельзя продолжить — причина видна в журнале бота."""


# --- трафик одного окна --------------------------------------------------------
class ClientFeed:
    """Разбор трафика одного клиента игры (одного локального UDP-порта)."""

    def __init__(self, make_radar: Callable, opcodes: dict | None = None, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.state = AlbionState(lambda *_a: None, opcodes, clock=clock)
        self.radar = make_radar()
        self.radar.attach(self.state)
        self.parser = PhotonParser(self._on_request, self.state.on_response, self.state.on_event,
                                   event_filter=self.state.accepts_event)
        self.requests = 0
        self.request_log: collections.deque = collections.deque(maxlen=30)
        self.request_counts: collections.Counter = collections.Counter()
        self.hits = 0                # сколько раз своё здоровье уменьшилось (урон)
        self.last_packet_at = 0.0
        self.harvests = 0
        self.hp: float | None = None
        self.max_hp: float | None = None
        self.state.on("event:harvest_finished", lambda _p: self._harvested())
        self.state.on("event:health_update", lambda p: self._health(p, 3, None))
        self.state.on("event:regeneration_health_changed", lambda p: self._health(p, 2, 3))

    def _harvested(self) -> None:
        self.harvests += 1

    def _health(self, p: dict, hkey: int, mkey: int | None) -> None:
        me = self.radar.me.get("id")
        if me is None or p.get(0) != me:
            return
        h = p.get(hkey)
        if isinstance(h, (int, float)) and not isinstance(h, bool):
            if self.hp is None or h < self.hp:
                self.hits += 1
            self.hp = float(h)
        m = p.get(mkey) if mkey is not None else None
        if isinstance(m, (int, float)) and not isinstance(m, bool) and m > 0:
            self.max_hp = float(m)

    def _on_request(self, code: int, params: dict) -> None:
        self.requests += 1
        real = self.state._code(params, code)
        name = self.state._op_names.get(real) or str(real)
        self.request_log.append((self.clock(), name))
        self.request_counts[name] += 1
        self.state.on_request(code, params)

    def feed(self, payload: bytes) -> None:
        self.last_packet_at = self.clock()
        self.parser.receive_packet(payload)
        # Встречи и узлы для базы этим копиям радара не нужны — не копим.
        self.radar.pending_players.clear()
        self.radar.pending_nodes.clear()

    @property
    def me(self) -> dict:
        with self.radar.lock:
            return dict(self.radar.me)

    @property
    def zone(self) -> str:
        return self.radar.me.get("zone") or ""

    @property
    def character(self) -> str:
        return self.radar.me.get("name") or self.state.character_name or ""

    @property
    def location(self) -> str:
        return self.state.location or self.state.zone

    @property
    def hp_pct(self) -> float | None:
        if self.hp is None or not self.max_hp:
            return None
        return max(0.0, min(100.0, 100.0 * self.hp / self.max_hp))

    def entities(self, kind: str | None = None) -> list:
        with self.radar.lock:
            return [e for e in list(self.radar.entities.values()) if kind is None or e.kind == kind]

    def entity(self, eid):
        with self.radar.lock:
            return self.radar.entities.get(eid)


# --- экран ↔ мир ---------------------------------------------------------------
@dataclass
class Calibration:
    """Где персонаж на экране и как смещение на экране переходит в смещение в мире.

    Смещение на экране меряется в долях высоты окна по обеим осям (не зависит от
    соотношения сторон). ``m`` — столбцы: мир на единицу смещения вправо и вниз."""

    cx: float = 0.5
    cy: float = 0.5
    m: list = field(default_factory=lambda: [[15.5, 21.9], [15.5, -21.9]])
    measured: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> Calibration:
        if not d:
            return cls()
        return cls(float(d.get("cx", 0.5)), float(d.get("cy", 0.5)),
                   [[float(v) for v in row] for row in d.get("m") or cls().m], bool(d.get("measured")))

    def to_world(self, sx: float, sy: float) -> tuple[float, float]:
        (a, b), (c, d) = self.m
        return a * sx + b * sy, c * sx + d * sy

    def to_screen(self, wx: float, wy: float) -> tuple[float, float]:
        (a, b), (c, d) = self.m
        det = a * d - b * c
        if abs(det) < 1e-9:
            raise BotError("калибровка испорчена — повторите её")
        return (d * wx - b * wy) / det, (-c * wx + a * wy) / det

    def fractions(self, sx: float, sy: float, aspect: float) -> tuple[float, float]:
        """Смещение (в долях высоты) → доли окна по x и y."""
        return self.cx + sx / max(aspect, 1e-6), self.cy + sy

    def clamp_step(self, sx: float, sy: float, aspect: float, step: float = STEP) -> tuple[float, float]:
        """Укоротить шаг до ``step`` и не выходить к краям окна."""
        n = math.hypot(sx, sy)
        if n > step:
            sx, sy = sx * step / n, sy * step / n
        lo_x, hi_x = (EDGE - self.cx) * aspect, (1 - EDGE - self.cx) * aspect
        lo_y, hi_y = EDGE - self.cy, 1 - EDGE - self.cy
        k = 1.0
        for v, lo, hi in ((sx, lo_x, hi_x), (sy, lo_y, hi_y)):
            if v > hi > 0:
                k = min(k, hi / v)
            elif v < lo < 0:
                k = min(k, lo / v)
        return sx * k, sy * k


def solve_calibration(d: float, plus_x, minus_x, plus_y, minus_y, cx=0.5, cy=0.5,
                      aspect: float = 16 / 9) -> Calibration:
    """Матрица по сдвигам персонажа после кликов на ±d по осям экрана.

    ``plus_x`` — сдвиг в мире после клика на +d по x, ``minus_x`` — после клика на −d
    (уже из новой точки) и так же по y. Разность пары даёт столбец матрицы, а сумма —
    насколько персонаж стоит не там, где считалось (``cx``, ``cy``): камера следует за
    персонажем, поэтому ошибка центра одинаково сдвигает оба клика пары."""
    col_x = ((plus_x[0] - minus_x[0]) / (2 * d), (plus_x[1] - minus_x[1]) / (2 * d))
    col_y = ((plus_y[0] - minus_y[0]) / (2 * d), (plus_y[1] - minus_y[1]) / (2 * d))
    nx, ny = math.hypot(*col_x), math.hypot(*col_y)
    if nx * d < 1.0 or ny * d < 1.0:
        raise BotError("персонаж почти не двигался от кликов — проверьте, что окно игры не свёрнуто, "
                       "клики доходят до игры (режим ввода) и центр персонажа указан верно")
    cos = (col_x[0] * col_y[0] + col_x[1] * col_y[1]) / (nx * ny)
    if abs(cos) > 0.87:
        raise BotError("клики по разным осям вели в одну сторону — персонаж упирался в препятствие? "
                       "Встаньте на открытое место и повторите")
    cal = Calibration(cx, cy, [[col_x[0], col_y[0]], [col_x[1], col_y[1]]], True)
    # Ошибка центра: M·e = (plus + minus) / 2 по каждой паре, берём среднее.
    ex = cal.to_screen((plus_x[0] + minus_x[0]) / 2, (plus_x[1] + minus_x[1]) / 2)
    ey = cal.to_screen((plus_y[0] + minus_y[0]) / 2, (plus_y[1] + minus_y[1]) / 2)
    off_x = max(-0.15, min(0.15, (ex[0] + ey[0]) / 2))
    off_y = max(-0.15, min(0.15, (ex[1] + ey[1]) / 2))
    cal.cx = round(min(0.85, max(0.15, cx - off_x / max(aspect, 1e-6))), 4)
    cal.cy = round(min(0.85, max(0.15, cy - off_y)), 4)
    return cal


# --- макросы -------------------------------------------------------------------
MACRO_HELP = """Строка — одно действие:
click 0.52 0.31        клик левой кнопкой (доли ширины и высоты окна)
click @search          клик по точке интерфейса из мастера точек
rclick 0.52 0.31       клик правой кнопкой
type {name}            ввести текст; подстановки {item} {name} {price} {qty}
key enter              клавиша или сочетание: ctrl+a, esc, tab, backspace…
wait 800               пауза, мс (к паузе добавляется немного случайности)
expect 3000            ждать, что игра отправит запрос серверу (иначе ошибка)
# комментарий"""

# Точки интерфейса игры, которые пользователь показывает кликом (мастер точек).
POINTS = {
    "market_npc": "Торговец рынка (персонаж стоит на месте «рынок»)",
    "sell_tab": "Вкладка «Продать» в окне рынка",
    "buy_tab": "Вкладка «Купить» в окне рынка",
    "search": "Поле поиска предмета",
    "first_item": "Первый предмет в результатах поиска",
    "sell_order": "Кнопка «Заказ на продажу» (Sell Order)",
    "buy_order": "Кнопка «Заказ на покупку» (Buy Order)",
    "price": "Поле цены за штуку",
    "qty": "Поле количества",
    "confirm": "Кнопка подтверждения заказа",
    "loot_all": "Кнопка «Взять всё» в окне добычи / сундука",
    "dungeon_enter": "Кнопка «Войти» при входе в данж (если игра спрашивает)",
    "stash_open": "Сундук в городе (персонаж стоит на месте сундука)",
    "stash_deposit": "Кнопка «Положить всё» в окне сундука",
}

# Готовые макросы из точек. {…} подставляет бот, @точка — координаты из мастера.
TEMPLATES = {
    "market_sell": """# Заказ на продажу: открыть рынок, найти предмет, выставить по цене
click @market_npc
wait 1500
click @sell_tab
wait 500
click @search
key ctrl+a
type {name}
wait 1200
click @first_item
wait 700
click @sell_order
wait 700
click @price
key ctrl+a
type {price}
click @qty
key ctrl+a
type {qty}
click @confirm
expect 3000
wait 800
key esc""",
    "market_buy": """# Заказ на покупку: открыть рынок, найти предмет, поставить заказ
click @market_npc
wait 1500
click @buy_tab
wait 500
click @search
key ctrl+a
type {name}
wait 1200
click @first_item
wait 700
click @buy_order
wait 700
click @price
key ctrl+a
type {price}
click @qty
key ctrl+a
type {qty}
click @confirm
expect 3000
wait 800
key esc""",
    "stash_deposit": """# Сдать добычу в сундук в городе
click @stash_open
wait 1500
click @stash_deposit
expect 3000
wait 800
key esc""",
    "loot_all": """# Забрать всё из открытой добычи
wait 400
click @loot_all
wait 600
key esc""",
}


def parse_macro(text: str, points: dict | None = None) -> list[tuple]:
    """Текст макроса → список шагов. Ошибка — ValueError с номером строки.

    ``points`` — точки мастера для ``click @имя``; без них проверяется только имя точки."""
    steps = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cmd, _, rest = line.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        try:
            if cmd in ("click", "rclick"):
                if rest.startswith("@"):
                    name = rest[1:].strip()
                    if name not in POINTS:
                        raise ValueError(f"нет такой точки «{name}»")
                    if points is not None:
                        if name not in points:
                            raise ValueError(f"точка «{POINTS[name]}» не указана — укажите её в мастере точек")
                        x, y = points[name]
                    else:
                        x, y = 0.5, 0.5
                else:
                    x, y = (float(v) for v in rest.split())
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    raise ValueError("координаты — доли окна от 0 до 1")
                steps.append((cmd, x, y))
            elif cmd == "type":
                steps.append(("type", rest))
            elif cmd == "key":
                parse_keys(rest)
                steps.append(("key", rest))
            elif cmd in ("wait", "expect"):
                ms = int(rest)
                if ms < 0 or ms > 600_000:
                    raise ValueError("время от 0 до 600000 мс")
                steps.append((cmd, ms))
            else:
                raise ValueError(f"неизвестное действие «{cmd}»")
        except ValueError as e:
            msg = str(e) if any(w in str(e) for w in ("«", "от ", "точк")) else "неверные параметры"
            raise ValueError(f"строка {n}: {msg}") from None
    return steps


def fill(text: str, values: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def market_price(best: float | None, side: str, undercut: float) -> int | None:
    """Цена заказа: продажа — на ``undercut`` ниже лучшей, покупка — выше."""
    if not best:
        return None
    price = best - undercut if side == "sell" else best + undercut
    return max(1, int(round(price)))


def parse_skills(text: str) -> list[tuple[str, float]]:
    """«q:3 w:10 e:20» → [(клавиша, перезарядка в секундах)]. Без «:» — 5 секунд."""
    out = []
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        if not part:
            continue
        key, _, cd = part.partition(":")
        parse_keys(key)
        try:
            cool = float(cd) if cd else 5.0
        except ValueError:
            raise ValueError(f"перезарядка «{cd}» — не число") from None
        out.append((key.lower(), max(0.2, cool)))
    return out


def split_items(text: str) -> list[str]:
    return [i.strip() for i in re.split(r"[\s,;]+", text or "") if i.strip()]
