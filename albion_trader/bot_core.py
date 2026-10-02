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
from typing import Callable, NamedTuple

from .activity import _fix
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
        self.items_put = 0           # предметов положено в сумку (InventoryPutItem)
        self.overloaded = False      # перегруз (OverloadModeUpdate / EncumberedRestricted)
        self.durability_events = 0
        self.money: float | None = None
        self.silver_gained = 0.0
        self.fame_gained = 0.0
        # Движение других (мобов, игроков) нужно боту всегда: кайт, угрозы.
        self.state.moves_until = float("inf")
        self.state.on("event:harvest_finished", lambda _p: self._harvested())
        self.state.on("event:inventory_put_item", lambda _p: self._put_item())
        self.state.on("event:overload_mode_update", self._overload)
        self.state.on("event:encumbered_restricted", lambda _p: setattr(self, "overloaded", True))
        self.state.on("event:durability_changed", lambda _p: self._durability())
        self.state.on("event:update_money", self._money)
        self.state.on("event:update_fame", self._fame)
        self.state.on("event:health_update", lambda p: self._health(p, 3, None))
        self.state.on("event:regeneration_health_changed", lambda p: self._health(p, 2, 3))

    def _harvested(self) -> None:
        self.harvests += 1

    def _put_item(self) -> None:
        self.items_put += 1

    def _durability(self) -> None:
        self.durability_events += 1

    def _overload(self, p: dict) -> None:
        me = self.radar.me.get("id")
        if p.get(0) not in (None, me):
            return
        v = p.get(1)
        self.overloaded = bool(v) if isinstance(v, (bool, int, float)) else True

    def _money(self, p: dict) -> None:
        """UpdateMoney: 1 — баланс серебра. Прирост копится (траты не вычитаются)."""
        silver = _fix(p.get(1))
        if silver is not None:
            if self.money is not None and silver > self.money:
                self.silver_gained += silver - self.money
            self.money = silver

    def _fame(self, p: dict) -> None:
        """UpdateFame: 2 — полученная слава (как в учёте активности)."""
        v = _fix(p.get(2))
        if v is not None and v > 0:
            self.fame_gained += v

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
    соотношения сторон). ``m`` — столбцы: мир на единицу смещения вправо и вниз.
    ``p`` — перспектива: камера наклонена, поэтому у верха экрана метров на долю
    больше, чем у низа; мир = M·s·(1 + p·sy)."""

    cx: float = 0.5
    cy: float = 0.5
    m: list = field(default_factory=lambda: [[15.5, 21.9], [15.5, -21.9]])
    measured: bool = False
    p: float = 0.0

    @classmethod
    def from_dict(cls, d: dict | None) -> Calibration:
        if not d:
            return cls()
        return cls(float(d.get("cx", 0.5)), float(d.get("cy", 0.5)),
                   [[float(v) for v in row] for row in d.get("m") or cls().m], bool(d.get("measured")),
                   float(d.get("p") or 0.0))

    def to_world(self, sx: float, sy: float) -> tuple[float, float]:
        (a, b), (c, d) = self.m
        k = 1.0 + self.p * sy
        return (a * sx + b * sy) * k, (c * sx + d * sy) * k

    def to_screen(self, wx: float, wy: float) -> tuple[float, float]:
        (a, b), (c, d) = self.m
        det = a * d - b * c
        if abs(det) < 1e-9:
            raise BotError("калибровка испорчена — повторите её")
        sx0, sy0 = (d * wx - b * wy) / det, (-c * wx + a * wy) / det
        p = self.p
        if abs(p) < 1e-9:
            return sx0, sy0
        # Перспектива верна только в пределах экрана: дальнюю цель решаем в масштабе
        # «полэкрана» и растягиваем обратно — направление сохраняется.
        n0 = math.hypot(sx0, sy0)
        f = 0.5 / n0 if n0 > 0.5 else 1.0
        sx0, sy0 = sx0 * f, sy0 * f
        # sy·(1 + p·sy) = sy0 — корень, близкий к sy0.
        disc = 1 + 4 * p * sy0
        sy = (-1 + math.sqrt(disc)) / (2 * p) if disc > 0 else sy0
        k = 1 + p * sy
        sx = sx0 / k if k > 0.2 else sx0
        if k <= 0.2:
            sy = sy0
        return sx / f, sy / f

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


def _fit_perspective(ts: list[float], gs: list[float]) -> tuple[float, float, float] | None:
    """Подбор c, e, p в g(t) = c·(t+e)·(1 + p·(t+e)) методом Гаусса — Ньютона."""
    c = (gs[0] - gs[1]) / (ts[0] - ts[1]) if ts[0] != ts[1] else 0.0
    if abs(c) < 1e-6:
        return None
    e, p = 0.0, 0.0
    for _ in range(30):
        jtj = [[0.0] * 3 for _ in range(3)]
        jtr = [0.0] * 3
        for t, gv in zip(ts, gs):
            u = t + e
            r = c * u * (1 + p * u) - gv
            j = (u * (1 + p * u), c * (1 + 2 * p * u), c * u * u)
            for a in range(3):
                jtr[a] += j[a] * r
                for b in range(3):
                    jtj[a][b] += j[a] * j[b]
        step = _solve3(jtj, jtr)
        if step is None:
            return None
        c, e, p = c - step[0], e - step[1], p - step[2]
        if max(abs(x) for x in step) < 1e-9:
            break
    return c, e, p


def _solve3(a: list[list[float]], b: list[float]) -> list[float] | None:
    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    dt = det(a)
    if abs(dt) < 1e-15:
        return None
    out = []
    for col in range(3):
        m = [row[:] for row in a]
        for r in range(3):
            m[r][col] = b[r]
        out.append(det(m) / dt)
    return out


def solve_calibration(d: float, plus_x, minus_x, plus_y, minus_y, cx=0.5, cy=0.5,
                      aspect: float = 16 / 9, plus_y2=None, minus_y2=None) -> Calibration:
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
    if plus_y2 is not None and minus_y2 is not None:
        # Перспектива по второй паре кликов (±2d по вертикали). Проекции сдвигов на ось y:
        # g(t) = c·u·(1 + p·u), u = t + e — подбираем c, e, p по четырём кликам.
        uy = (col_y[0] / ny, col_y[1] / ny)
        g = [m[0] * uy[0] + m[1] * uy[1] for m in (plus_y, minus_y, plus_y2, minus_y2)]
        fit = _fit_perspective([d, -d, 2 * d, -2 * d], g)
        if fit is not None:
            c, e, p = fit
            cal.p = round(max(-0.9, min(0.9, p)), 4)
            k0 = 1 + cal.p * e                  # клики по x шли на высоте ошибки центра
            cal.m = [[col_x[0] / k0, uy[0] * c], [col_x[1] / k0, uy[1] * c]]
            off_y = max(-0.15, min(0.15, e))
            ex = cal.to_screen((plus_x[0] + minus_x[0]) / 2, (plus_x[1] + minus_x[1]) / 2)
            off_x = max(-0.15, min(0.15, ex[0]))
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


class Skill(NamedTuple):
    """Умение: клавиша, перезарядка и условия применения."""
    key: str
    cd: float
    hp_below: float | None = None     # только если своё здоровье ниже, %
    hp_above: float | None = None     # только если своё здоровье выше, %
    boss: bool = False                # только против босса
    cast: float = 0.0                 # время каста: не двигаться, с
    self_cast: bool = False           # не нужна цель (лечение, щит)
    opener: bool = False              # в начале боя с целью


SKILLS_HELP = """Умение — «клавиша:перезарядка», через пробел, в порядке приоритета.
Условия через @: hp<50 (своё здоровье ниже 50%), hp>80, boss (только по боссу),
cast=1.5 (каст 1,5 с — не двигаться), self (без цели, например лечение),
open (первым в бою с целью). Пример: q:3 w:10@open e:20@boss 2:30@hp<40@self"""


def parse_skills(text: str) -> list[Skill]:
    """«q:3 w:10@open e:20@boss r:30@hp<50@self f:15@cast=1.5» → умения. Без «:» — 5 с."""
    out = []
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        if not part:
            continue
        head, *conds = part.split("@")
        key, _, cd = head.partition(":")
        parse_keys(key)
        try:
            cool = float(cd) if cd else 5.0
        except ValueError:
            raise ValueError(f"перезарядка «{cd}» — не число") from None
        opts: dict = {}
        for c in conds:
            c = c.strip().lower()
            m = re.fullmatch(r"hp([<>])(\d+(?:\.\d+)?)", c)
            mc = re.fullmatch(r"cast=(\d+(?:\.\d+)?)", c)
            if m:
                opts["hp_below" if m.group(1) == "<" else "hp_above"] = float(m.group(2))
            elif mc:
                opts["cast"] = min(10.0, float(mc.group(1)))
            elif c in ("boss", "self", "open"):
                opts[{"boss": "boss", "self": "self_cast", "open": "opener"}[c]] = True
            else:
                raise ValueError(f"непонятное условие «@{c}» у «{key}»")
        out.append(Skill(key.lower(), max(0.2, cool), **opts))
    return out


def skill_ready(s: Skill, hp_pct: float | None, boss: bool, first: bool) -> bool:
    """Подходит ли умение сейчас (без учёта перезарядки)."""
    if s.boss and not boss:
        return False
    if s.opener and not first:
        return False
    if s.hp_below is not None and (hp_pct is None or hp_pct >= s.hp_below):
        return False
    if s.hp_above is not None and hp_pct is not None and hp_pct <= s.hp_above:
        return False
    return True


def split_items(text: str) -> list[str]:
    return [i.strip() for i in re.split(r"[\s,;]+", text or "") if i.strip()]
