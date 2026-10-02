"""Боты «живости» для своего сервера: несколько окон игры собирают ресурсы и торгуют.

Как это устроено:

* **Чей трафик.** У каждого окна игры свой процесс и свой локальный UDP-порт.
  Пакеты игры раскладываются по портам, для каждого порта — свой разбор Photon
  (:class:`ClientFeed`): своя позиция, зона, персонаж и объекты вокруг (тот же
  :class:`~albion_trader.radar.Radar`, что и у радара). Порт → процесс → окно
  сопоставляется средствами Windows (:mod:`albion_trader.bot_win`).
* **Куда кликать.** Камера игры повёрнута и наклонена, поэтому смещение на
  экране переводится в смещение в мире матрицей 2×2. Калибровка находит её сама:
  бот кликает рядом с персонажем по двум осям и смотрит, куда тот пошёл. Ходьба
  идёт короткими шагами с проверкой позиции, так что неточность матрицы не страшна.
* **Сбор.** Ближайший подходящий ресурс (вид, тир, зачарование, радиус от
  точки старта, нет врагов рядом) → подойти → кликнуть по нему → ждать, пока
  узел не истощится. Нет ресурсов — прогулка по округе.
* **Рынок.** Интерфейс рынка у всех разный (разрешение, масштаб), поэтому
  действия записываются макросом: клики в долях окна, ввод текста с
  подстановками ``{item}``/``{name}``/``{price}``/``{qty}``, клавиши, паузы и
  проверка «игра отправила запрос». Цена — из собранных программой цен рынка.
* **Безопасность.** Все действия ботов идут по очереди через одну блокировку
  ввода. Пока человек двигает мышью или печатает, боты ждут. Клавиша остановки
  (по умолчанию F12) останавливает всех.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .bot_win import VK, VK_LBUTTON, Desktop, GameWindow, parse_keys
from .capture.albion import AlbionState
from .capture.photon import PhotonParser

log = logging.getLogger("albion_trader.bots")

VK_RBUTTON = 0x02
RES_KINDS = ("wood", "rock", "fiber", "hide", "ore")
WALK_SPEED = 5.0          # м/с пешком — оценка времени шага
STEP = 0.26               # самый длинный шаг-клик, доля высоты окна
EDGE = 0.07               # не кликать ближе к краю окна (там интерфейс)
NEAR_NODE = 3.5           # с какого расстояния кликать по самому узлу, м
HARVEST_IDLE = 12.0       # узел не меняется столько секунд — сбор не идёт
FAILED_KEEP = 300.0       # не возвращаться к узлу, который не удалось собрать, с
MAX_LOG = 200
CLEARANCE = 3.5           # клик для ходьбы — не ближе к чужому ресурсу, м
# Варианты шага в обход чужого ресурса: (доля длины, поворот в радианах).
CLEAR_TRIES = [(1.0, 0.0), (0.6, 0.0), (1.0, 0.5), (1.0, -0.5), (0.8, 1.0), (0.8, -1.0),
               (0.6, 1.57), (0.6, -1.57), (0.4, 2.3), (0.4, -2.3)]

DEFAULT_GATHER = {"res": list(RES_KINDS), "tier_min": 2, "tier_max": 8, "enchant_min": 0,
                  "radius": 80, "avoid_players": True, "avoid_mobs": 0, "max_nodes": 0}
DEFAULT_MARKET = {"items": "", "side": "sell", "undercut": 1, "qty": 1,
                  "interval_min": 2.0, "interval_max": 6.0,
                  "macro_open": "", "macro_order": "", "macro_close": ""}
DEFAULT_BOT = {"task": "gather", "input": "focus", "work_min": 25, "rest_min": 5,
               "calib": None, "gather": DEFAULT_GATHER, "market": DEFAULT_MARKET}
DEFAULT_CONFIG = {"enabled": False, "stop_key": "f12", "pause_when_active": True, "user_idle": 3.0,
                  "restore_focus": True, "bots": {}, "macros": {}}
TASKS = {"gather": "сбор ресурсов", "market": "рынок", "mixed": "сбор и рынок по очереди",
         "wander": "прогулка"}


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
        self.parser = PhotonParser(self._on_request, self.state.on_response, self._on_event,
                                   event_filter=self.state.accepts_event)
        self.requests = 0
        self.request_log: collections.deque = collections.deque(maxlen=30)
        self.last_packet_at = 0.0
        self.moved_at = 0.0          # когда последний раз менялась своя позиция
        self.harvests = 0
        self.state.on("request:move", lambda _p: self._moved())
        self.state.on("response:join", lambda _p: self._moved())
        self.state.on("event:harvest_finished", lambda _p: self._harvested())

    def _moved(self) -> None:
        self.moved_at = self.clock()

    def _harvested(self) -> None:
        self.harvests += 1

    def _on_request(self, code: int, params: dict) -> None:
        self.requests += 1
        real = self.state._code(params, code)
        self.request_log.append((self.clock(), self.state._op_names.get(real) or str(real)))
        self.state.on_request(code, params)

    def _on_event(self, code: int, params: dict) -> None:
        self.state.on_event(code, params)

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
    def character(self) -> str:
        return self.radar.me.get("name") or self.state.character_name or ""

    @property
    def location(self) -> str:
        return self.state.location or self.state.zone

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
rclick 0.52 0.31       клик правой кнопкой
type {name}            ввести текст; подстановки {item} {name} {price} {qty}
key enter              клавиша или сочетание: ctrl+a, esc, tab, backspace…
wait 800               пауза, мс (к паузе добавляется немного случайности)
expect 3000            ждать, что игра отправит запрос серверу (иначе ошибка)
# комментарий"""


def parse_macro(text: str) -> list[tuple]:
    """Текст макроса → список шагов. Ошибка — ValueError с номером строки."""
    steps = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cmd, _, rest = line.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        try:
            if cmd in ("click", "rclick"):
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
            msg = str(e) if "«" in str(e) or "от " in str(e) else "неверные параметры"
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


# --- бот -----------------------------------------------------------------------
class Bot:
    def __init__(self, manager: BotManager, pid: int):
        self.manager = manager
        self.pid = pid
        self.task = ""
        self.status = "остановлен"
        self.log: collections.deque = collections.deque(maxlen=MAX_LOG)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.home: tuple[float, float] | None = None
        self.failed: dict[int, float] = {}
        self.fails_in_row = 0
        self.home_zone = ""
        self.gathered = 0
        self.orders = 0
        self.rng = random.Random()

    # --- окружение ------------------------------------------------------
    @property
    def clock(self):
        return self.manager.clock

    @property
    def feed(self) -> ClientFeed:
        feed = self.manager.feed_for(self.pid)
        if feed is None:
            raise BotError("нет трафика этого окна — войдите в игру и смените зону, чтобы бот увидел персонажа")
        return feed

    @property
    def window(self) -> GameWindow:
        win = self.manager.windows.get(self.pid)
        if win is None:
            raise BotError("окно игры закрыто")
        return win

    @property
    def name(self) -> str:
        return self.manager.character_of(self.pid)

    @property
    def cfg(self) -> dict:
        return self.manager.bot_config(self.name)

    @property
    def calib(self) -> Calibration:
        return Calibration.from_dict(self.cfg.get("calib"))

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def note(self, text: str) -> None:
        self.log.append({"ts": self.clock(), "text": text})
        log.info("Бот %s: %s", self.name or self.pid, text)

    def wait(self, seconds: float) -> None:
        if self.manager.sleep(max(0.0, seconds), self.stop_event):
            raise BotStopped()

    def check(self) -> None:
        if self.stop_event.is_set():
            raise BotStopped()

    # --- запуск ---------------------------------------------------------
    def start(self, task: str) -> None:
        if self.running:
            raise BotError("бот уже работает")
        if task not in TASKS and task != "calibrate":
            raise BotError(f"неизвестная задача «{task}»")
        self.stop_event.clear()
        self.task = task
        self.thread = threading.Thread(target=self.run, args=(task,), daemon=True, name=f"bot-{self.pid}")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self, task: str) -> None:
        self.task = task
        self.status = "запуск"
        try:
            if task == "calibrate":
                self.calibrate()
            else:
                self.work(task)
            self.status = "готово"
        except BotStopped:
            self.status = "остановлен"
            self.note("остановлен")
        except BotError as e:
            self.status = "ошибка"
            self.note(f"ошибка: {e}")
        except Exception as e:  # pragma: no cover - неожиданная ошибка не должна молча убить бота
            self.status = "ошибка"
            self.note(f"сбой: {e}")
            log.exception("Сбой бота %s", self.pid)

    # --- ввод -----------------------------------------------------------
    def act(self, fn: Callable[[Desktop, GameWindow, bool], None]) -> None:
        """Выполнить действие ввода по очереди с другими ботами."""
        self.manager.act(self, fn)

    def click_at(self, fx: float, fy: float, button: str = "left") -> None:
        jx, jy = self.rng.uniform(-0.004, 0.004), self.rng.uniform(-0.004, 0.004)
        self.act(lambda d, w, bg: d.click(w, fx + jx, fy + jy, button, background=bg))

    def plan_click(self, dx: float, dy: float, step: float = STEP) -> tuple[float, float, float, float]:
        """Куда кликнуть, чтобы пойти к смещению (dx, dy) от персонажа: доли окна и
        смещение в мире, которое реально получится (шаг ограничен)."""
        win, cal = self.window, self.calib
        aspect = win.rect[2] / max(win.rect[3], 1)
        sx, sy = cal.clamp_step(*cal.to_screen(dx, dy), aspect, step)
        fx, fy = cal.fractions(sx, sy, aspect)
        wx, wy = cal.to_world(sx, sy)
        return fx, fy, wx, wy

    def click_world(self, dx: float, dy: float, step: float = STEP, keep_clear_of=None) -> tuple[float, float]:
        """Клик по точке в мире со смещением (dx, dy) от персонажа (шаг ограничен).

        ``keep_clear_of`` — id ресурса, к которому идём: клик для ходьбы не должен попасть
        в другой ресурс (игра начала бы собирать его), поэтому шаг укорачивается или
        поворачивается. Возвращает смещение в мире, по которому кликнули."""
        fx, fy, wx, wy = self.plan_click(dx, dy, step)
        if keep_clear_of is not False:
            px, py = self.pos()
            others = [e for e in self.feed.entities("resource") if e.id != keep_clear_of]
            if others:
                best, best_gap = None, -1.0
                for k, angle in CLEAR_TRIES:
                    c, s_ = math.cos(angle), math.sin(angle)
                    plan = self.plan_click(dx * c - dy * s_, dx * s_ + dy * c, step * k)
                    gap = min(math.hypot(e.x - px - plan[2], e.y - py - plan[3]) for e in others)
                    if gap > best_gap:
                        best, best_gap = plan, gap
                    if gap > CLEARANCE:
                        break
                fx, fy, wx, wy = best
        self.click_at(fx, fy)
        return wx, wy

    # --- движение -------------------------------------------------------
    def pos(self) -> tuple[float, float]:
        me = self.feed.me
        return me["x"], me["y"]

    def settle(self, timeout: float = 4.0, quiet: float = 0.8) -> tuple[float, float]:
        """Дождаться, пока персонаж остановится (позиция не меняется ``quiet`` секунд)."""
        start = self.clock()
        last, since = self.pos(), self.clock()
        while self.clock() - start < timeout:
            self.wait(0.2)
            p = self.pos()
            if math.hypot(p[0] - last[0], p[1] - last[1]) > 0.05:
                last, since = p, self.clock()
            elif self.clock() - since >= quiet:
                break
        return last

    def walk_to(self, x: float, y: float, tol: float = 2.0, max_steps: int = 40, target=None) -> bool:
        stuck = 0
        for _ in range(max_steps):
            self.check()
            px, py = self.pos()
            dx, dy = x - px, y - py
            dist = math.hypot(dx, dy)
            if dist <= tol:
                return True
            wx, wy = self.click_world(dx, dy, keep_clear_of=target)
            self.status = f"идёт ({dist:.0f} м)"
            self.wait(min(4.0, math.hypot(wx, wy) / WALK_SPEED + 0.3) * self.rng.uniform(0.7, 0.9))
            nx, ny = self.pos()
            if math.hypot(nx - px, ny - py) < 0.4:
                stuck += 1
                if stuck >= 3:
                    # Препятствие: шаг в сторону.
                    ang = self.rng.uniform(0, 2 * math.pi)
                    self.click_world(6 * math.cos(ang), 6 * math.sin(ang), keep_clear_of=target)
                    self.wait(1.5)
                if stuck >= 6:
                    return False
            else:
                stuck = 0
        return False

    # --- калибровка -----------------------------------------------------
    def calibrate(self, d: float = 0.15) -> Calibration:
        self.status = "калибровка"
        cfg = self.cfg
        old = Calibration.from_dict(cfg.get("calib"))
        aspect = self.window.rect[2] / max(self.window.rect[3], 1)
        self.note("калибровка: клики рядом с персонажем по двум осям — встаньте на открытое место")
        px, py = self.settle()
        near = [e for e in self.feed.entities("resource") if math.hypot(e.x - px, e.y - py) < 7]
        if near:
            raise BotError("рядом ресурс — клик калибровки начнёт его сбор; отойдите на открытое место "
                           "(в 7 м вокруг не должно быть ресурсов)")
        moves = []
        for sx, sy in ((d, 0), (-d, 0), (0, d), (0, -d)):
            before = self.settle(timeout=2.0)
            requests = self.feed.requests
            self.click_at(*old.fractions(sx, sy, aspect))
            self.wait(0.6)
            after = self.settle()
            if self.feed.requests == requests and not moves:
                raise BotError("игра не отправила ни одного запроса после клика — клики не доходят до окна "
                               "(попробуйте режим ввода «с переключением окна» и запуск программы от администратора)")
            moves.append((after[0] - before[0], after[1] - before[1]))
        cal = solve_calibration(d, moves[0], moves[1], moves[2], moves[3], old.cx, old.cy, aspect)
        self.manager.update_bot(self.name, {"calib": asdict(cal)})
        self.note("калибровка готова: вправо — ({:.1f}, {:.1f}) м, вниз — ({:.1f}, {:.1f}) м на высоту окна, "
                  "персонаж в точке ({:.2f}, {:.2f}) окна".format(
                      cal.m[0][0], cal.m[1][0], cal.m[0][1], cal.m[1][1], cal.cx, cal.cy))
        return cal

    # --- работа ---------------------------------------------------------
    def work(self, task: str) -> None:
        cfg = self.cfg
        if task in ("gather", "mixed", "wander") and not self.calib.measured:
            self.note("калибровки нет — сначала калибровка")
            self.calibrate()
        self.home, self.home_zone = self.pos(), self.feed.me.get("zone", "")
        self.fails_in_row = 0
        self.note(f"старт: {TASKS[task]}")
        phase = "market" if task == "market" else "gather" if task != "wander" else "wander"
        while True:
            self.check()
            work_s = max(1.0, float(cfg.get("work_min") or 25)) * 60 * self.rng.uniform(0.8, 1.2)
            until = self.clock() + work_s
            if phase == "market":
                self.market_session(until)
            else:
                self.gather_session(until, wander_only=phase == "wander")
            rest = max(0.0, float(cfg.get("rest_min") or 0)) * 60 * self.rng.uniform(0.6, 1.4)
            if rest:
                self.status = f"отдых {rest / 60:.0f} мин"
                self.note(self.status)
                self.wait(rest)
            if task == "mixed":
                phase = "market" if phase == "gather" else "gather"
                if phase == "market":
                    self.note("переход к рынку: поставьте персонажа у рынка в городе — макрос «открыть» "
                              "должен открывать рынок с этого места")

    # --- сбор -----------------------------------------------------------
    def danger(self, g: dict) -> str:
        if not g.get("avoid_players"):
            return ""
        px, py = self.pos()
        for e in self.feed.entities("player"):
            if e.faction == 255 and math.hypot(e.x - px, e.y - py) < 50:
                return e.name or "враждебный игрок"
        return ""

    def pick_node(self, g: dict):
        px, py = self.pos()
        hx, hy = self.home or (px, py)
        now = self.clock()
        mobs = self.feed.entities("mob") if g.get("avoid_mobs") else []
        best, best_d = None, 1e18
        for e in self.feed.entities("resource"):
            if e.res not in (g.get("res") or RES_KINDS) or e.size == 0:
                continue
            if not (int(g.get("tier_min", 1)) <= (e.tier or 0) <= int(g.get("tier_max", 8))):
                continue
            if (e.enchant or 0) < int(g.get("enchant_min", 0)):
                continue
            if self.failed.get(e.id, 0) > now:
                continue
            if math.hypot(e.x - hx, e.y - hy) > float(g.get("radius", 80)):
                continue
            if any(math.hypot(m.x - e.x, m.y - e.y) < float(g["avoid_mobs"]) for m in mobs):
                continue
            d = math.hypot(e.x - px, e.y - py)
            if d < best_d:
                best, best_d = e, d
        return best

    def gather_session(self, until: float, wander_only: bool = False) -> None:
        g = {**DEFAULT_GATHER, **(self.cfg.get("gather") or {})}
        while self.clock() < until:
            self.check()
            zone = self.feed.me.get("zone", "")
            if zone and zone != self.home_zone:
                self.home, self.home_zone = self.pos(), zone
                self.note(f"сменилась зона ({zone}) — точка старта теперь здесь")
            if self.fails_in_row >= 3:
                raise BotError("три узла подряд не собираются — сумка полна, нет нужного инструмента "
                               "или тир ресурса выше навыка")
            if g.get("max_nodes") and self.gathered >= int(g["max_nodes"]):
                self.note(f"собрано узлов: {self.gathered} — норма выполнена, дальше прогулка")
                wander_only = True
            enemy = self.danger(g)
            if enemy:
                self.status = f"рядом враг: {enemy} — отход"
                self.note(self.status)
                if self.home:
                    self.walk_to(*self.home, tol=4)
                self.wait(self.rng.uniform(8, 15))
                continue
            node = None if wander_only else self.pick_node(g)
            if node is None:
                self.wander(g)
                continue
            self.harvest(node)

    def wander(self, g: dict) -> None:
        hx, hy = self.home or self.pos()
        r = float(g.get("radius", 80)) * 0.6
        ang, dist = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(5, max(6.0, r))
        self.status = "прогулка"
        self.walk_to(hx + dist * math.cos(ang), hy + dist * math.sin(ang), tol=3, max_steps=15)
        self.wait(self.rng.uniform(2, 8))

    def harvest(self, node) -> bool:
        name = f"{node.res} T{node.tier}" + (f".{node.enchant}" if node.enchant else "")
        self.status = f"к ресурсу {name}"
        if not self.walk_to(node.x, node.y, tol=NEAR_NODE, target=node.id):
            self.failed[node.id] = self.clock() + FAILED_KEEP
            self.note(f"не дошёл до {name} — пропускаю")
            return False
        px, py = self.pos()
        before_harvests = self.feed.harvests
        self.click_world(node.x - px, node.y - py, step=0.5, keep_clear_of=False)
        self.status = f"сбор {name}"
        size = node.size
        changed = self.clock()
        while True:
            self.wait(0.5)
            cur = self.feed.entity(node.id)
            if cur is None or cur.size == 0:
                break
            if cur.size != size:
                size, changed = cur.size, self.clock()
            if self.feed.harvests != before_harvests:
                before_harvests, changed = self.feed.harvests, self.clock()
            if self.clock() - changed > HARVEST_IDLE:
                self.failed[node.id] = self.clock() + FAILED_KEEP
                self.fails_in_row += 1
                self.note(f"{name}: сбор не идёт (нет инструмента, мал тир или мешает объект) — пропускаю")
                return False
        self.gathered += 1
        self.fails_in_row = 0
        self.note(f"собран {name} (всего {self.gathered})")
        self.wait(self.rng.uniform(0.5, 2.5))
        return True

    # --- рынок ----------------------------------------------------------
    def run_macro(self, name: str, values: dict) -> None:
        text = self.manager.config["macros"].get(name)
        if text is None:
            raise BotError(f"нет макроса «{name}» — запишите его во вкладке «Боты»")
        try:
            steps = parse_macro(text)
        except ValueError as e:
            raise BotError(f"макрос «{name}», {e}") from None
        mark = self.feed.requests
        for step in steps:
            self.check()
            cmd = step[0]
            if cmd in ("click", "rclick", "type", "key"):
                # «expect» засчитывает запросы с последнего действия ввода: игра
                # отвечает на клик сразу, раньше, чем начнётся ожидание.
                mark = self.feed.requests
            if cmd in ("click", "rclick"):
                self.click_at(step[1], step[2], "right" if cmd == "rclick" else "left")
                self.wait(self.rng.uniform(0.15, 0.35))
            elif cmd == "type":
                text_ = fill(step[1], values)
                self.act(lambda d, w, bg, t=text_: d.type_text(w, t, background=bg))
            elif cmd == "key":
                self.act(lambda d, w, bg, k=step[1]: d.press(w, k, background=bg))
                self.wait(0.1)
            elif cmd == "wait":
                self.wait(step[1] / 1000 * self.rng.uniform(1.0, 1.25))
            elif cmd == "expect":
                start = self.clock()
                while self.feed.requests == mark:
                    if self.clock() - start > step[1] / 1000:
                        raise BotError(f"макрос «{name}»: игра не отправила запрос — интерфейс не там, "
                                       "где записан (другое разрешение или окно рынка не открыто?)")
                    self.wait(0.2)
                mark = self.feed.requests

    def market_items(self, m: dict) -> list[str]:
        return [i.strip() for i in re.split(r"[\s,;]+", m.get("items") or "") if i.strip()]

    def market_session(self, until: float) -> None:
        m = {**DEFAULT_MARKET, **(self.cfg.get("market") or {})}
        items = self.market_items(m)
        if not items:
            raise BotError("рынок: не задан список предметов")
        if not m.get("macro_order"):
            raise BotError("рынок: не выбран макрос заказа")
        if m.get("macro_open"):
            self.status = "открывает рынок"
            self.run_macro(m["macro_open"], {})
        try:
            while self.clock() < until:
                self.check()
                item = self.rng.choice(items)
                best = self.manager.price_of(item, self.feed.location, m.get("side", "sell"))
                price = market_price(best, m.get("side", "sell"), float(m.get("undercut") or 0))
                if price is None:
                    self.note(f"{item}: нет цены на этом рынке — пропускаю (откройте рынок в игре, "
                              "чтобы программа собрала цены)")
                else:
                    values = {"item": item, "name": self.manager.item_name(item), "price": price,
                              "qty": int(m.get("qty") or 1)}
                    self.status = f"заказ {item} по {price}"
                    self.run_macro(m["macro_order"], values)
                    self.orders += 1
                    self.note(f"{'продажа' if m.get('side', 'sell') == 'sell' else 'покупка'}: {values['name']} "
                              f"× {values['qty']} по {price} (заказов: {self.orders})")
                lo = float(m.get("interval_min") or 1)
                hi = max(lo, float(m.get("interval_max") or lo))
                self.status = "ждёт до следующего заказа"
                self.wait(self.rng.uniform(lo, hi) * 60)
        finally:
            if m.get("macro_close") and not self.stop_event.is_set():
                self.run_macro(m["macro_close"], {})

    def snapshot(self) -> dict:
        return {"task": self.task, "status": self.status, "running": self.running,
                "gathered": self.gathered, "orders": self.orders, "log": list(self.log)[-40:]}


# --- запись макроса ------------------------------------------------------------
class Recorder:
    """Записывает клики человека в окне игры как строки макроса."""

    def __init__(self, pid: int, clock):
        self.pid = pid
        self.clock = clock
        self.lines: list[str] = []
        self.last_at = clock()
        self.down = {VK_LBUTTON: False, VK_RBUTTON: False}

    def poll(self, desktop: Desktop, win: GameWindow | None) -> None:
        if win is None:
            return
        for vk, cmd in ((VK_LBUTTON, "click"), (VK_RBUTTON, "rclick")):
            pressed = desktop.key_down(vk)
            if pressed and not self.down[vk] and desktop.foreground() == win.hwnd:
                x, y = desktop.cursor()
                rx, ry, w, h = win.rect
                if rx <= x < rx + w and ry <= y < ry + h and w > 1 and h > 1:
                    now = self.clock()
                    gap = int(round((now - self.last_at) * 10)) * 100
                    if self.lines and gap >= 200:
                        self.lines.append(f"wait {min(gap, 10000)}")
                    self.lines.append(f"{cmd} {(x - rx) / (w - 1):.4f} {(y - ry) / (h - 1):.4f}")
                    self.last_at = now
            self.down[vk] = pressed

    def text(self) -> str:
        return "\n".join(self.lines)


# --- менеджер ------------------------------------------------------------------
def _event_sleep(seconds: float, event: threading.Event) -> bool:
    return event.wait(seconds)


class BotManager:
    def __init__(self, config_path: str | Path, make_radar: Callable, desktop: Desktop | None = None,
                 opcodes: Callable[[], dict | None] = lambda: None,
                 price_of: Callable[[str, str, str], float | None] = lambda *_a: None,
                 item_name: Callable[[str], str] = lambda i: i,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float, threading.Event], bool] = _event_sleep):
        self.config_path = Path(config_path)
        self.make_radar = make_radar
        self.desktop = desktop or Desktop()
        self.opcodes = opcodes
        self.price_of = price_of
        self.item_name = item_name
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.RLock()
        self.input_lock = threading.Lock()
        self.windows: dict[int, GameWindow] = {}
        self.feeds: dict[int, ClientFeed] = {}       # локальный порт → разбор
        self.port_pid: dict[int, int] = {}
        self.names: dict[int, str] = {}              # pid → последний известный персонаж
        self.bots: dict[int, Bot] = {}
        self.recorder: Recorder | None = None
        self.message = ""
        self.config = self._load()
        self._loop: threading.Thread | None = None
        self._loop_stop = threading.Event()
        self._refreshed = 0.0

    # --- настройки ------------------------------------------------------
    def _load(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("Не удалось прочитать %s: %s", self.config_path, e)
        return cfg

    def save(self) -> None:
        with self.lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.config_path)

    def bot_config(self, name: str) -> dict:
        with self.lock:
            saved = self.config["bots"].get(name) or {}
            out = {**json.loads(json.dumps(DEFAULT_BOT)), **saved}
            out["gather"] = {**DEFAULT_GATHER, **(saved.get("gather") or {})}
            out["market"] = {**DEFAULT_MARKET, **(saved.get("market") or {})}
            return out

    def update_bot(self, name: str, values: dict) -> dict:
        if not name:
            raise BotError("персонаж окна ещё не известен — смените зону в игре")
        with self.lock:
            cur = self.config["bots"].setdefault(name, {})
            for k, v in values.items():
                if k in ("gather", "market") and isinstance(v, dict):
                    allowed = DEFAULT_GATHER if k == "gather" else DEFAULT_MARKET
                    cur[k] = {**(cur.get(k) or {}), **{kk: vv for kk, vv in v.items() if kk in allowed}}
                elif k in DEFAULT_BOT:
                    cur[k] = v
            self.save()
            return self.bot_config(name)

    # --- трафик ---------------------------------------------------------
    def on_packet(self, local_port: int, payload: bytes) -> None:
        """Пакет игры, локальный порт — наша сторона соединения (вызывается захватом)."""
        if not self.config.get("enabled"):
            return
        feed = self.feeds.get(local_port)
        if feed is None:
            with self.lock:
                feed = self.feeds.get(local_port)
                if feed is None:
                    if len(self.feeds) > 64:      # старые порты закрытых окон
                        oldest = min(self.feeds, key=lambda p: self.feeds[p].last_packet_at)
                        del self.feeds[oldest]
                    feed = self.feeds[local_port] = ClientFeed(self.make_radar, self.opcodes(), self.clock)
        try:
            feed.feed(payload)
        except Exception:  # pragma: no cover - битый пакет не роняет захват
            log.debug("Ошибка разбора пакета бота", exc_info=True)

    def feed_for(self, pid: int) -> ClientFeed | None:
        win = self.windows.get(pid)
        if win is None:
            return None
        feeds = [self.feeds[p] for p in win.ports if p in self.feeds]
        if not feeds:
            return None
        return max(feeds, key=lambda f: (bool(f.character), f.last_packet_at))

    def character_of(self, pid: int) -> str:
        feed = self.feed_for(pid)
        name = feed.character if feed else ""
        if name:
            self.names[pid] = name
        return name or self.names.get(pid, "")

    # --- окна -----------------------------------------------------------
    def refresh(self) -> None:
        try:
            wins = self.desktop.game_windows()
        except Exception as e:  # pragma: no cover - ошибка Windows не роняет программу
            log.debug("Не удалось получить окна игры", exc_info=True)
            self.message = f"не удалось получить список окон: {e}"
            return
        with self.lock:
            self.windows = {w.pid: w for w in wins}
            for pid in list(self.bots):
                if pid not in self.windows:
                    self.bots[pid].stop()
            self.port_pid = {p: w.pid for w in wins for p in w.ports}
        self._refreshed = self.clock()

    def bot(self, pid: int) -> Bot:
        with self.lock:
            if pid not in self.windows:
                raise BotError("окно игры не найдено")
            b = self.bots.get(pid)
            if b is None:
                b = self.bots[pid] = Bot(self, pid)
            return b

    # --- ввод -----------------------------------------------------------
    def act(self, bot: Bot, fn) -> None:
        cfg = self.config
        while True:
            bot.check()
            if cfg.get("pause_when_active") and self.desktop.idle_seconds() < float(cfg.get("user_idle") or 3):
                prev = bot.status
                bot.status = "пауза: вы за компьютером"
                bot.wait(1.0)
                bot.status = prev
                continue
            with self.input_lock:
                bot.check()
                win = bot.window
                if not self.desktop.window_alive(win.hwnd):
                    raise BotError("окно игры закрыто")
                background = bot.cfg.get("input") == "background"
                prev_fg = self.desktop.foreground()
                prev_cursor = self.desktop.cursor()
                if not background and not self.desktop.focus(win.hwnd):
                    raise BotError("Windows не дала переключиться на окно игры")
                fn(self.desktop, win, background)
                if cfg.get("restore_focus") and not background and prev_fg and prev_fg != win.hwnd:
                    self.desktop.sleep(0.05)
                    self.desktop.focus(prev_fg)
                    self.desktop.move_cursor(*prev_cursor)
            return

    # --- фоновый цикл: клавиша остановки, запись, список окон -------------
    def start_loop(self) -> None:
        if self._loop and self._loop.is_alive():
            return
        self._loop_stop.clear()
        self._loop = threading.Thread(target=self._run_loop, daemon=True, name="bots")
        self._loop.start()

    def stop_loop(self) -> None:
        self._loop_stop.set()
        self.stop_all()

    def _run_loop(self) -> None:
        while not self._loop_stop.wait(0.1):
            try:
                self.tick()
            except Exception:  # pragma: no cover
                log.exception("Ошибка цикла ботов")

    def tick(self) -> None:
        if not self.config.get("enabled") or not self.desktop.available:
            return
        if self.clock() - self._refreshed > 2.0:
            self.refresh()
        stop_vk = VK.get(str(self.config.get("stop_key") or "f12").lower())
        if stop_vk and any(b.running for b in self.bots.values()) and self.desktop.key_down(stop_vk):
            self.stop_all()
            self.message = f"все боты остановлены клавишей {self.config.get('stop_key', 'f12').upper()}"
            log.info(self.message)
        rec = self.recorder
        if rec is not None:
            rec.poll(self.desktop, self.windows.get(rec.pid))

    def stop_all(self) -> None:
        for b in list(self.bots.values()):
            b.stop()

    # --- команды из интерфейса ------------------------------------------
    ACTIONS = {"settings", "stop_all", "save_macro", "delete_macro", "record_stop", "record_start", "configure",
               "start", "calibrate", "stop", "test_click"}

    def command(self, body: dict) -> dict:
        action = body.get("action")
        if action not in self.ACTIONS:
            raise BotError(f"неизвестная команда «{action}»")
        pid = body.get("pid")
        pid = int(pid) if pid not in (None, "") else None
        if action == "settings":
            with self.lock:
                for k in ("enabled", "stop_key", "pause_when_active", "user_idle", "restore_focus"):
                    if k in body:
                        v = body[k]
                        if k == "stop_key":
                            v = str(v).lower()
                            if v not in VK:
                                raise BotError(f"неизвестная клавиша «{v}»")
                        if k == "user_idle":
                            v = max(0.0, min(60.0, float(v)))
                        self.config[k] = v
                self.save()
            if self.config["enabled"]:
                self.start_loop()
                self.refresh()
            else:
                self.stop_all()
            return {"ok": True}
        if action == "stop_all":
            self.stop_all()
            return {"ok": True}
        if action == "save_macro":
            name = (body.get("name") or "").strip()
            if not name or len(name) > 60:
                raise BotError("укажите имя макроса")
            try:
                parse_macro(body.get("text") or "")
            except ValueError as e:
                raise BotError(str(e)) from None
            with self.lock:
                self.config["macros"][name] = body.get("text") or ""
                self.save()
            return {"ok": True}
        if action == "delete_macro":
            with self.lock:
                self.config["macros"].pop(body.get("name") or "", None)
                self.save()
            return {"ok": True}
        if action == "record_stop":
            rec, self.recorder = self.recorder, None
            return {"ok": True, "text": rec.text() if rec else ""}
        if pid is None:
            raise BotError("не указано окно")
        if action == "record_start":
            self.bot(pid)
            self.recorder = Recorder(pid, self.clock)
            return {"ok": True}
        if action == "configure":
            name = self.character_of(pid)
            values = {k: body[k] for k in ("task", "input", "work_min", "rest_min", "gather", "market", "calib")
                      if k in body}
            if "input" in values and values["input"] not in ("focus", "background"):
                raise BotError("режим ввода: focus или background")
            if "task" in values and values["task"] not in TASKS:
                raise BotError("неизвестная задача")
            return {"ok": True, "config": self.update_bot(name, values)}
        b = self.bot(pid)
        if action == "start":
            if not b.name:
                raise BotError("персонаж окна ещё не известен — смените зону в игре, чтобы бот увидел вход")
            b.start(body.get("task") or b.cfg.get("task") or "gather")
            return {"ok": True}
        if action == "calibrate":
            if not b.name:
                raise BotError("персонаж окна ещё не известен — смените зону в игре, чтобы бот увидел вход")
            b.start("calibrate")
            return {"ok": True}
        if action == "stop":
            b.stop()
            return {"ok": True}
        if action == "test_click":
            fx, fy = float(body.get("x", 0.5)), float(body.get("y", 0.5))
            threading.Thread(target=self._test_click, args=(b, fx, fy), daemon=True).start()
        return {"ok": True}

    def _test_click(self, b: Bot, fx: float, fy: float) -> None:
        try:
            before = b.feed.requests if self.feed_for(b.pid) else None
            b.click_at(fx, fy)
            b.wait(1.0)
            after = b.feed.requests if self.feed_for(b.pid) else None
            if before is None:
                b.note("пробный клик сделан; трафика окна пока нет")
            else:
                b.note("пробный клик: игра " + ("ответила запросом серверу" if after != before
                                                 else "не отправила запрос (клик не дошёл или мимо)"))
        except (BotError, BotStopped) as e:
            b.note(f"пробный клик не удался: {e}")

    # --- состояние для интерфейса ---------------------------------------
    def snapshot(self) -> dict:
        if self.config.get("enabled") and self.clock() - self._refreshed > 2.0:
            self.refresh()
        now = self.clock()
        out = []
        for pid, win in sorted(self.windows.items()):
            feed = self.feed_for(pid)
            name = self.character_of(pid)
            me = feed.me if feed else {}
            b = self.bots.get(pid)
            res = feed.entities("resource") if feed else []
            out.append({
                "pid": pid, "window": win.to_dict(), "character": name,
                "traffic": bool(feed and now - feed.last_packet_at < 30),
                "zone": me.get("zone", ""), "location": feed.location if feed else "",
                "pos": [round(me.get("x", 0), 1), round(me.get("y", 0), 1)] if feed else None,
                "resources": len(res), "players": len(feed.entities("player")) if feed else 0,
                "requests": feed.requests if feed else 0,
                "config": self.bot_config(name) if name else None,
                "bot": b.snapshot() if b else {"status": "остановлен", "running": False, "log": [],
                                                "gathered": 0, "orders": 0, "task": ""},
            })
        return {"supported": self.desktop.available, "enabled": bool(self.config.get("enabled")),
                "settings": {k: self.config.get(k) for k in ("stop_key", "pause_when_active", "user_idle",
                                                             "restore_focus")},
                "windows": out, "macros": self.config["macros"], "recording": self.recorder.pid if self.recorder
                else None, "record_text": self.recorder.text() if self.recorder else "",
                "message": self.message, "tasks": TASKS, "macro_help": MACRO_HELP,
                "keys": sorted(VK)}
