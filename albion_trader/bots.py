"""Бот для своего сервера: один бот за раз, окно игры выбирается само.

Задачи: **сбор ресурсов**, **рынок** (заказы по ценам собранных данных),
**перевозка** между городами (погрузка в А → дорога по зонам → разгрузка в Б) и
**данж** (бой с мобами, сундуки, добыча). Подробности задач — в
:mod:`albion_trader.bot_tasks`, общее — в :mod:`albion_trader.bot_core`,
навигация — в :mod:`albion_trader.bot_nav`, Windows — в :mod:`albion_trader.bot_win`.

Как устроено:

* **Какое окно.** Бот работает с окном игры, активным в момент запуска (если
  активно не оно — с тем, где последним шёл трафик). Окно → процесс → его UDP-порт,
  поэтому трафик других окон не мешает.
* **Куда кликать.** Калибровка: 4 клика рядом с персонажем, по которым считается,
  как экран переходит в координаты мира и где на экране персонаж. Ходьба идёт
  короткими шагами с проверкой позиции; по схеме зоны — в обход препятствий;
  между зонами — по маршруту через выходы.
* **Интерфейс игры** (рынок, «взять всё») — по точкам, которые пользователь один раз
  показывает кликом в окне игры (мастер точек). Из точек собираются готовые макросы;
  можно писать и свои.
* **Безопасность.** Пока человек двигает мышью или печатает, бот ждёт; клавиша
  остановки (по умолчанию F12) останавливает его.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import math
import random
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .bot_core import (MACRO_HELP, POINTS, SKILLS_HELP, TEMPLATES, BotError, BotStopped, Calibration, ClientFeed,
                       fill, parse_macro, parse_skills, solve_calibration, split_items)
from .bot_nav import Grid, Router
from .bot_schedule import SCHEDULE_HELP, current, next_start, parse_schedule
from .bot_session import Session
from .bot_vision import LABELS, PROBE_POINTS, OcrError, data_url, find_points, ocr_windows, png_encode, probe_offsets, \
    scale2x
from .bot_dungeon import PORTAL_KINDS, DungeonMixin
from .bot_tasks import DEFAULTS, TasksMixin
from .bot_win import VK, VK_LBUTTON, Desktop, GameWindow

log = logging.getLogger("albion_trader.bots")

VK_RBUTTON = 0x02
WALK_SPEED = 5.0          # м/с пешком — оценка времени шага
MAX_LOG = 200
CLEARANCE = 3.5           # клик для ходьбы — не ближе к чужому ресурсу, м
# Варианты шага в обход чужого ресурса: (доля длины, поворот в радианах).
CLEAR_TRIES = [(1.0, 0.0), (0.6, 0.0), (1.0, 0.5), (1.0, -0.5), (0.8, 1.0), (0.8, -1.0),
               (0.6, 1.57), (0.6, -1.57), (0.4, 2.3), (0.4, -2.3)]
ZONE_WAIT = 20.0          # сколько ждать загрузки следующей зоны, с

TASKS = {"gather": "Сбор ресурсов", "market": "Рынок", "transport": "Перевозка между городами",
         "dungeon_run": "Данжи по кругу из города", "dungeon": "Пройти этот данж", "wander": "Прогулка",
         "schedule": "По расписанию"}
SAFETY_NAMES = {"safe": "только безопасные (синие)", "yellow": "с жёлтыми", "red": "с красными",
                "black": "любые, включая чёрные"}
DEFAULT_CONFIG = {"enabled": False, "stop_key": "f12", "pause_when_active": True, "user_idle": 3.0,
                  "restore_focus": True, "input": "focus", "task": "gather", "work_min": 25, "rest_min": 5,
                  "watchdog_min": 10, "record": False, "schedule": "", "profiles": {}, "history": [],
                  "calib": {}, "points": {}, "points_size": "", "places": {}, "macros": {},
                  **{k: dict(v) for k, v in DEFAULTS.items()}}
SETTINGS = ("enabled", "stop_key", "pause_when_active", "user_idle", "restore_focus", "input", "task",
            "work_min", "rest_min", "watchdog_min", "record", "schedule")


def size_key(win: GameWindow) -> str:
    return f"{win.rect[2]}x{win.rect[3]}"


# --- бот -----------------------------------------------------------------------
class RouteDanger(Exception):
    """На пути игроки — перестроить маршрут."""


class TaskTimeUp(Exception):
    """Окно расписания закончилось — пора к следующей задаче."""


PROFILE_KEYS = ("task", "work_min", "rest_min", "input", *DEFAULTS)
MAX_HISTORY = 50


class Bot(TasksMixin, DungeonMixin):
    def __init__(self, manager: BotManager):
        self.manager = manager
        self.pid: int | None = None
        self.task = ""
        self.status = "остановлен"
        self.log: collections.deque = collections.deque(maxlen=MAX_LOG)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.home: tuple[float, float] | None = None
        self.home_zone = ""
        self.failed: dict[int, float] = {}
        self.fails_in_row = 0
        self.gathered = self.orders = self.trips = self.kills = self.loots = self.runs = 0
        self.bad_portals: set = set()
        self.start_dungeon_state()
        self.rng = random.Random()
        self.heat_visited: dict = {}
        self.items_base = 0
        self.spent = 0.0
        self.route_guard: dict | None = None     # настройки угроз в пути (перевозка)
        self.deadline: float | None = None       # конец окна расписания
        self.started_at = 0.0
        self.stats_base = (0.0, 0.0)             # серебро и слава в окне игры на старте
        self.deaths = 0
        self.probe_name = ""
        self.avoid_exits: dict = {}              # (зона, x, y) → до какого времени не идти
        # Сторож: когда последний раз что-то менялось (позиция, зона, добыча…).
        self.progress_at = 0.0
        self._progress_sig = None
        self.stuck_tries = 0
        self.recovering = False
        self.idle = False              # законное ожидание (отдых, пауза рынка) — не зависание
        # Что бот сейчас делает — для отображения на радаре.
        self.plan: dict = {"zone": "", "target": None, "path": [], "explored": []}

    # --- окружение ------------------------------------------------------
    @property
    def clock(self):
        return self.manager.clock

    @property
    def feed(self) -> ClientFeed:
        feed = self.manager.feed_for(self.pid)
        if feed is None:
            raise BotError("нет трафика окна игры — смените зону, чтобы бот увидел персонажа")
        return feed

    @property
    def window(self) -> GameWindow:
        win = self.manager.windows.get(self.pid)
        if win is None:
            raise BotError("окно игры закрыто")
        return win

    def task_cfg(self, name: str) -> dict:
        return self.manager.task_config(name)

    @property
    def calib(self) -> Calibration:
        return Calibration.from_dict(self.manager.config["calib"].get(size_key(self.window)))

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def note(self, text: str) -> None:
        self.log.append({"ts": self.clock(), "text": text})
        log.info("Бот: %s", text)
        self.manager.record({"a": "note", "text": text})

    def alert(self, key: str, title: str, text: str) -> None:
        """Важное событие — в оповещения программы (Windows, Telegram, Discord)."""
        try:
            self.manager.notify(key, title, text)
        except Exception:  # pragma: no cover - оповещение не должно ронять бота
            log.debug("Не удалось отправить оповещение бота", exc_info=True)

    def wait(self, seconds: float) -> None:
        if self.deadline is not None:
            seconds = min(seconds, max(0.0, self.deadline - self.clock()) + 0.01)
        if self.manager.sleep(max(0.0, seconds), self.stop_event):
            raise BotStopped()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise TaskTimeUp()
        self.watch_progress()

    def wait_idle(self, seconds: float) -> None:
        """Ожидание, которое не считается зависанием (отдых, пауза между заказами)."""
        self.idle = True
        try:
            self.wait(seconds)
        finally:
            self.idle = False
            self.progress_at = self.clock()

    # --- сторож от зависаний ----------------------------------------------
    def watch_progress(self) -> None:
        feed = self.manager.feed_for(self.pid)
        if feed is None:
            return
        me = feed.me
        sig = (me.get("zone"), int(me.get("x", 0) // 5), int(me.get("y", 0) // 5), self.gathered, self.orders,
               self.trips, self.kills, self.loots, self.runs)
        now = self.clock()
        if sig != self._progress_sig or self.idle:
            self._progress_sig, self.progress_at = sig, now
            if not self.idle:
                self.stuck_tries = 0
            return
        limit = float(self.manager.config.get("watchdog_min") or 0) * 60
        if not limit or self.recovering or now - self.progress_at < limit:
            return
        self.recovering = True
        try:
            self.stuck_tries += 1
            if self.stuck_tries > 2:
                raise BotError(f"нет прогресса {limit / 60:.0f} мин даже после попыток выбраться — бот остановлен")
            self.note(f"нет прогресса {limit / 60:.0f} мин — пробую выбраться")
            self.recover()
        finally:
            self.recovering = False
            self.progress_at = self.clock()

    def recover(self) -> None:
        """Выбраться из тупика: в данже — быстрый выход, иначе шаги в разные стороны."""
        d = self.task_cfg("dungeon")
        if self.floors and d.get("exit_key") and self.quick_exit(d):
            return
        for _ in range(3):
            ang = self.rng.uniform(0, 2 * math.pi)
            self.click_world(10 * math.cos(ang), 10 * math.sin(ang))
            self.wait(2.0)

    def check(self) -> None:
        if self.stop_event.is_set():
            raise BotStopped()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise TaskTimeUp()

    # --- запуск ---------------------------------------------------------
    def start(self, task: str, pid: int) -> None:
        if task not in TASKS and task not in ("calibrate", "probe"):
            raise BotError(f"неизвестная задача «{task}»")
        if self.running:
            self.stop()
            self.thread.join(5)
        self.stop_event.clear()
        self.pid, self.task = pid, task
        self.progress_at, self._progress_sig, self.stuck_tries = self.clock(), None, 0
        self.thread = threading.Thread(target=self.run, args=(task,), daemon=True, name="bot")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self, task: str) -> None:
        self.task = task
        self.status = "запуск"
        self.started_at = self.clock()
        self.gathered = self.orders = self.trips = self.kills = self.loots = self.runs = 0
        self.spent = 0.0
        feed = self.manager.feed_for(self.pid)
        self.stats_base = (feed.silver_gained, feed.fame_gained) if feed else (0.0, 0.0)
        self.manager.open_session(task)
        try:
            if task == "calibrate":
                self.calibrate()
            elif task == "probe":
                self.probe(self.probe_name)
            else:
                self.work(task)
            self.status = "готово"
        except BotStopped:
            self.status = "остановлен"
            self.note("остановлен")
        except BotError as e:
            self.status = "ошибка"
            if "погиб" in str(e):
                self.deaths += 1
            self.note(f"ошибка: {e}")
            self.alert(f"error:{e}", "Бот остановлен", str(e))
        except Exception as e:  # pragma: no cover - неожиданная ошибка не должна молча убить бота
            self.status = "ошибка"
            self.note(f"сбой: {e}")
            log.exception("Сбой бота")
        finally:
            self.plan = {"zone": "", "target": None, "path": [], "explored": []}
            self.deadline = None
            self.manager.close_session()
            if task not in ("calibrate", "probe"):
                self.manager.add_history(self.stats())

    def work(self, task: str) -> None:
        needs_moves = task != "market" or bool(self.task_cfg("market").get("place"))
        if needs_moves and not self.calib.measured:
            self.note("калибровки нет — сначала калибровка")
            self.calibrate()
        self.home, self.home_zone = self.pos(), self.feed.zone
        self.fails_in_row = 0
        self.items_base = self.feed.items_put
        self.route_guard = None
        self.note(f"старт: {TASKS[task]}")
        if task == "transport":
            self.transport()
            return
        if task == "dungeon":
            self.dungeon()
            return
        if task == "dungeon_run":
            self.dungeon_run()
            return
        if task == "schedule":
            self.run_schedule()
            return
        cfg = self.manager.config
        limit = int(self.task_cfg("market").get("orders") or 0) if task == "market" else 0
        while True:
            self.check()
            until = self.clock() + max(1.0, float(cfg.get("work_min") or 25)) * 60 * self.rng.uniform(0.8, 1.2)
            if task == "market":
                self.market_session(until)
                if limit and self.orders >= limit:
                    return
            else:
                self.gather_session(until, wander_only=task == "wander")
            rest = max(0.0, float(cfg.get("rest_min") or 0)) * 60 * self.rng.uniform(0.6, 1.4)
            if rest:
                self.status = f"отдых {rest / 60:.0f} мин"
                self.note(self.status)
                self.wait_idle(rest)

    # --- ввод -----------------------------------------------------------
    def act(self, fn: Callable[[Desktop, GameWindow, bool], None]) -> None:
        self.manager.act(self, fn)

    def click_at(self, fx: float, fy: float, button: str = "left", jitter: bool = True) -> None:
        jx, jy = (self.rng.uniform(-0.004, 0.004), self.rng.uniform(-0.004, 0.004)) if jitter else (0.0, 0.0)
        self.manager.record({"a": "click", "x": round(fx + jx, 4), "y": round(fy + jy, 4), "b": button})
        self.act(lambda d, w, bg: d.click(w, fx + jx, fy + jy, button, background=bg))

    def press_key(self, combo: str) -> None:
        self.manager.record({"a": "key", "k": combo})
        self.act(lambda d, w, bg: d.press(w, combo, background=bg))

    def plan_click(self, dx: float, dy: float, step: float) -> tuple[float, float, float, float]:
        """Куда кликнуть, чтобы пойти к смещению (dx, dy) от персонажа: доли окна и
        смещение в мире, которое реально получится (шаг ограничен)."""
        win, cal = self.window, self.calib
        aspect = win.rect[2] / max(win.rect[3], 1)
        sx, sy = cal.clamp_step(*cal.to_screen(dx, dy), aspect, step)
        fx, fy = cal.fractions(sx, sy, aspect)
        wx, wy = cal.to_world(sx, sy)
        return fx, fy, wx, wy

    def click_world(self, dx: float, dy: float, step: float = 0.26, keep_clear_of=None) -> tuple[float, float]:
        """Клик по точке в мире со смещением (dx, dy) от персонажа (шаг ограничен).

        ``keep_clear_of`` — id объекта, к которому идём (или None): клик для ходьбы не
        должен попасть в другой ресурс (игра начала бы собирать его) или в портал/выход
        (игра начала бы переход), поэтому шаг укорачивается или поворачивается.
        ``False`` — клик ровно в цель (по объекту)."""
        fx, fy, wx, wy = self.plan_click(dx, dy, step)
        if keep_clear_of is not False:
            px, py = self.pos()
            others = [e for e in self.feed.entities("resource") + self.exits_around() if e.id != keep_clear_of]
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

    # --- макросы --------------------------------------------------------
    def run_template(self, name: str, values: dict) -> None:
        self.run_steps(f"шаблон «{name}»", TEMPLATES[name], values)

    def run_macro(self, name: str, values: dict) -> None:
        text = self.manager.config["macros"].get(name)
        if text is None:
            raise BotError(f"нет макроса «{name}» — запишите его во вкладке «Боты»")
        self.run_steps(f"макрос «{name}»", text, values)

    def run_steps(self, title: str, text: str, values: dict) -> None:
        try:
            steps = parse_macro(text, self.manager.config["points"])
        except ValueError as e:
            raise BotError(f"{title}, {e}") from None
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
                self.manager.record({"a": "type", "text": text_})
                self.act(lambda d, w, bg, t=text_: d.type_text(w, t, background=bg))
            elif cmd == "key":
                self.press_key(step[1])
                self.wait(0.1)
            elif cmd == "wait":
                self.wait(step[1] / 1000 * self.rng.uniform(1.0, 1.25))
            elif cmd == "expect":
                start = self.clock()
                while self.feed.requests == mark:
                    if self.clock() - start > step[1] / 1000:
                        raise BotError(f"{title}: игра не отправила запрос — интерфейс не там, где указан "
                                       "(другой размер окна или окно рынка не открылось?)")
                    self.wait(0.2)
                mark = self.feed.requests

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

    def walk_to(self, x: float, y: float, tol: float = 2.0, max_steps: int | None = None, target=None) -> bool:
        if max_steps is None:
            px, py = self.pos()
            max_steps = int(math.hypot(x - px, y - py) / 4) + 20
        stuck = 0
        zone = self.feed.zone
        if self.plan.get("zone") != zone:
            self.plan = {"zone": zone, "target": None, "path": [], "explored": []}
        self.plan["target"] = [round(x, 1), round(y, 1)]
        for _ in range(max_steps):
            self.check()
            if self.feed.zone != zone:
                return False          # ушли в другую зону (выход по дороге)
            if self.route_guard is not None and getattr(self, "travelling", False):
                threats = self.threats(self.route_guard)
                if threats:
                    raise RouteDanger(threats)
            px, py = self.pos()
            dx, dy = x - px, y - py
            dist = math.hypot(dx, dy)
            if dist <= tol:
                return True
            wx, wy = self.click_world(dx, dy, keep_clear_of=target)
            if not self.status.startswith("в пути"):
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

    def walk_path(self, x: float, y: float, tol: float = 2.5) -> bool:
        """Дойти до точки зоны в обход препятствий по схеме зоны (если она есть)."""
        grid = self.manager.zone_grid(self.feed.zone)
        if grid is not None:
            pts = grid.path(self.pos(), (x, y))
            if pts:
                zone = self.feed.zone
                self.plan = {**self.plan, "zone": zone, "path": [[round(a, 1), round(b, 1)] for a, b in pts]}
                for wx, wy in pts[:-1]:
                    self.walk_to(wx, wy, tol=3.0)
                    if self.feed.zone != zone:
                        return False
        return self.walk_to(x, y, tol=tol)

    def wait_zone(self, old: str, timeout: float = ZONE_WAIT) -> bool:
        start = self.clock()
        while self.clock() - start < timeout:
            if self.feed.zone and self.feed.zone != old:
                self.wait(1.5)        # зона загрузилась — пусть персонаж появится
                return True
            self.wait(0.5)
        return False

    def travel_to(self, zone: str, x: float | None = None, y: float | None = None, safety: str = "safe") -> None:
        """Дойти до точки (x, y) в зоне ``zone``, при необходимости через другие зоны
        (без точки — только войти в зону). С ``route_guard`` (перевозка) игроки на пути
        к выходу заставляют убежать и перестроить маршрут через другой выход."""
        router = self.manager.router()
        for _ in range(80):
            self.check()
            cur = self.feed.zone
            if not cur:
                raise BotError("зона неизвестна — смените зону в игре, чтобы бот её увидел")
            if cur == zone:
                if x is None:
                    return
                if not self.walk_path(x, y):
                    px, py = self.pos()
                    if self.feed.zone == zone and math.hypot(x - px, y - py) > 6:
                        raise BotError(f"не удалось дойти до точки в «{router.name(zone)}»")
                    if self.feed.zone != zone:
                        continue
                return
            now = self.clock()
            avoid = [k for k, until in self.avoid_exits.items() if until > now]
            try:
                hops = router.route(cur, zone, safety, self.pos(), avoid_exits=avoid)
            except ValueError as e:
                if avoid:               # обхода нет — ждать и идти прежним путём
                    self.note("обхода нет — пережидаю и иду прежним путём")
                    self.wait_idle(self.rng.uniform(20, 40))
                    self.avoid_exits.clear()
                    continue
                raise BotError(str(e)) from None
            _z, ex, ey, nxt = hops[0]
            self.status = f"в пути: {router.name(nxt)} (осталось переходов: {len(hops)})"
            try:
                self.travelling = True
                for _walk in range(3):        # дойти до выхода (с повтором, если не дошли)
                    self.walk_path(ex, ey, tol=1.5)
                    px, py = self.pos()
                    if self.feed.zone != cur or math.hypot(ex - px, ey - py) <= 5:
                        break
            except RouteDanger as danger:
                threats = danger.args[0]
                guard = self.route_guard or {}
                self.avoid_exits[(cur, round(ex), round(ey))] = now + float(guard.get("avoid_min") or 15) * 60
                what = ", ".join(t.text() for t in threats[:3])
                self.note(f"на пути игроки: {what} — ищу обход")
                self.alert(f"route:{cur}:{int(now // 600)}", "Бот: игроки на пути", what)
                self.travelling = False
                self.avoid_players(guard)
                continue
            finally:
                self.travelling = False
            if self.feed.zone == cur and not self.wait_zone(cur, 6):
                for _try in range(3):     # встать точно на выход
                    px, py = self.pos()
                    self.click_world(ex - px, ey - py, step=0.3, keep_clear_of=False)
                    if self.wait_zone(cur, 8):
                        break
                else:
                    raise BotError(f"не удалось перейти в «{router.name(nxt)}» — выход закрыт или не там")
            self.note(f"перешёл в «{router.name(self.feed.zone)}»")
        raise BotError("слишком много переходов — маршрут зациклился")

    def go_place(self, name: str, safety: str = "safe") -> None:
        place = self.manager.config["places"].get(name)
        if not place:
            raise BotError(f"нет сохранённого места «{name}» — встаньте там и нажмите «Запомнить место»")
        self.travel_to(place["zone"], float(place["x"]), float(place["y"]), safety=safety)

    # --- калибровка -----------------------------------------------------
    def calibrate(self, d: float = 0.15) -> Calibration:
        self.status = "калибровка"
        win = self.window
        old = self.calib
        aspect = win.rect[2] / max(win.rect[3], 1)
        self.note("калибровка: клики рядом с персонажем по двум осям — встаньте на открытое место")
        px, py = self.settle()
        near = [e for e in self.feed.entities("resource") if math.hypot(e.x - px, e.y - py) < 7]
        if near:
            raise BotError("рядом ресурс — клик калибровки начнёт его сбор; отойдите на открытое место "
                           "(в 7 м вокруг не должно быть ресурсов)")
        moves = []
        for sx, sy in ((d, 0), (-d, 0), (0, d), (0, -d), (0, 2 * d), (0, -2 * d)):
            before = self.settle(timeout=2.0)
            requests = self.feed.requests
            self.click_at(*old.fractions(sx, sy, aspect), jitter=False)
            self.wait(0.6)
            after = self.settle()
            if self.feed.requests == requests and not moves:
                raise BotError("игра не отправила ни одного запроса после клика — клики не доходят до окна "
                               "(попробуйте ввод «с переключением окна» и запуск программы от администратора)")
            moves.append((after[0] - before[0], after[1] - before[1]))
        if not getattr(self.feed.state, "_move_seen", True):
            raise BotError("игра отвечает на клики, но запрос движения ещё не опознан — своя позиция не "
                           "обновляется. Пробегитесь персонажем 5–10 секунд и повторите калибровку; если не поможет — "
                           "пришлите таблицу «Запросы игры» (Радар → Коды событий)")
        cal = solve_calibration(d, moves[0], moves[1], moves[2], moves[3], old.cx, old.cy, aspect,
                                moves[4], moves[5])
        with self.manager.lock:
            self.manager.config["calib"][size_key(win)] = asdict(cal)
            self.manager.save()
        self.note("калибровка готова: вправо — ({:.1f}, {:.1f}) м, вниз — ({:.1f}, {:.1f}) м на высоту окна, "
                  "персонаж в точке ({:.2f}, {:.2f}) окна, перспектива {:+.2f}".format(
                      cal.m[0][0], cal.m[1][0], cal.m[0][1], cal.m[1][1], cal.cx, cal.cy, cal.p))
        return cal

    # --- автоопределение пробными кликами ----------------------------------
    def probe(self, name: str) -> tuple[float, float]:
        """Найти торговца / сундук рядом: кликать по кругу около персонажа, пока игра не
        отправит запрос, отличный от движения (открылось окно)."""
        label = POINTS[name]
        self.status = f"ищет: {label}"
        self.note(f"автоопределение: {label} — пробные клики рядом с персонажем")
        win, cal = self.window, self.calib
        aspect = win.rect[2] / max(win.rect[3], 1)
        start = self.settle()
        # Что игра шлёт сама, без кликов (пинги и т. п.) — это не признак.
        idle0 = collections.Counter(self.feed.request_counts)
        self.wait(2.0)
        background = {k for k, v in (self.feed.request_counts - idle0).items() if v}
        for sx, sy in probe_offsets():
            self.check()
            before = collections.Counter(self.feed.request_counts)
            fx, fy = cal.fractions(sx, sy, aspect)
            if not (0.05 < fx < 0.95 and 0.05 < fy < 0.95):
                continue
            self.click_at(fx, fy, jitter=False)
            self.wait(1.5)
            new = {k for k, v in (self.feed.request_counts - before).items() if v and k != "move"} - background
            if new:
                point = (round(fx, 4), round(fy, 4))
                with self.manager.lock:
                    self.manager.config["points"][name] = list(point)
                    self.manager.config["points_size"] = size_key(win)
                    self.manager.save()
                self.note(f"найдено: {label} — точка {point[0]:.3f}, {point[1]:.3f} (игра ответила: {', '.join(sorted(new))})")
                self.press_key("esc")
                return point
            px, py = self.pos()
            if math.hypot(px - start[0], py - start[1]) > 0.8:
                self.walk_to(*start, tol=0.6, max_steps=6)
                self.settle()
        raise BotError(f"не нашёл «{label}» рядом — встаньте вплотную и повторите или укажите точку на снимке")

    # --- расписание -----------------------------------------------------
    def run_schedule(self) -> None:
        try:
            windows = parse_schedule(self.manager.config.get("schedule") or "", TASKS.keys() - {"schedule"})
        except ValueError as e:
            raise BotError(str(e)) from None
        if not windows:
            raise BotError("расписание пустое — заполните его на вкладке «Боты»")
        while True:
            self.check()
            now = dt.datetime.fromtimestamp(self.clock())
            win = current(windows, now)
            if win is None:
                nxt = next_start(windows, now)
                self.status = f"по расписанию: ждёт до {nxt:%a %H:%M}" if nxt else "по расписанию: ждёт"
                self.wait_idle(60)
                continue
            if win.character:
                pid = self.manager.window_of(win.character)
                if pid is None:
                    self.note(f"окно с персонажем «{win.character}» не найдено — жду")
                    self.wait_idle(60)
                    continue
                self.pid = pid
            end = win.ends_at(now)
            self.note(f"по расписанию: {TASKS[win.task]} до {end:%H:%M}" + (f" ({win.character})" if win.character else ""))
            # Конец окна — через разницу времени (timestamp() в Windows не работает для старых дат).
            ends = self.clock() + (end - now).total_seconds()
            self.deadline = ends
            try:
                self.work(win.task)
            except TaskTimeUp:
                self.note("окно расписания закончилось")
            except BotError as e:
                self.note(f"ошибка в задаче по расписанию: {e} — жду следующего окна")
                self.alert(f"schedule:{e}", "Бот: ошибка по расписанию", str(e))
                self.deadline = None
                self.wait_idle(max(60.0, ends - self.clock()))
            finally:
                self.deadline = None

    # --- статистика -------------------------------------------------------
    def stats(self) -> dict:
        feed = self.manager.feed_for(self.pid)
        silver = (feed.silver_gained - self.stats_base[0]) if feed else 0.0
        fame = (feed.fame_gained - self.stats_base[1]) if feed else 0.0
        minutes = max(0.0, (self.clock() - self.started_at) / 60) if self.started_at else 0.0
        hours = max(minutes / 60, 1e-9)
        return {"task": self.task, "status": self.status, "start": round(self.started_at), "minutes": round(minutes, 1),
                "gathered": self.gathered, "orders": self.orders, "trips": self.trips, "kills": self.kills,
                "loots": self.loots, "runs": self.runs, "deaths": self.deaths, "silver": round(silver),
                "fame": round(fame), "silver_hour": round(silver / hours) if minutes >= 1 else None,
                "fame_hour": round(fame / hours) if minutes >= 1 else None,
                "min_per_run": round(minutes / self.runs, 1) if self.runs else None}

    def snapshot(self) -> dict:
        return {"task": self.task, "status": self.status, "running": self.running,
                "stats": {"gathered": self.gathered, "orders": self.orders, "trips": self.trips,
                          "kills": self.kills, "loots": self.loots, "runs": self.runs, "floors": len(self.floors)},
                "session": self.stats() if self.started_at else None,
                "log": list(self.log)[-50:]}


# --- запись макроса и точек ---------------------------------------------------
class Recorder:
    """Записывает клики человека в окне игры: весь макрос или одну точку интерфейса."""

    def __init__(self, pid: int, clock, point: str = ""):
        self.pid = pid
        self.clock = clock
        self.point = point            # имя точки мастера: запись до первого клика
        self.lines: list[str] = []
        self.captured: tuple[float, float] | None = None
        self.last_at = clock()
        self.down = {VK_LBUTTON: False, VK_RBUTTON: False}

    def poll(self, desktop: Desktop, win: GameWindow | None) -> None:
        if win is None or self.captured is not None:
            return
        for vk, cmd in ((VK_LBUTTON, "click"), (VK_RBUTTON, "rclick")):
            pressed = desktop.key_down(vk)
            if pressed and not self.down[vk] and desktop.foreground() == win.hwnd:
                x, y = desktop.cursor()
                rx, ry, w, h = win.rect
                if rx <= x < rx + w and ry <= y < ry + h and w > 1 and h > 1:
                    fx, fy = (x - rx) / (w - 1), (y - ry) / (h - 1)
                    if self.point:
                        self.captured = (round(fx, 4), round(fy, 4))
                        return
                    now = self.clock()
                    gap = int(round((now - self.last_at) * 10)) * 100
                    if self.lines and gap >= 200:
                        self.lines.append(f"wait {min(gap, 10000)}")
                    self.lines.append(f"{cmd} {fx:.4f} {fy:.4f}")
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
                 zonemaps=None, zone_name: Callable[[str], str] | None = None,
                 notify: Callable[[str, str, str], None] = lambda *_a: None,
                 heat: Callable[[str], list] = lambda _z: [],
                 my_orders: Callable[[str, str, str], list] = lambda *_a: [],
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float, threading.Event], bool] = _event_sleep):
        self.config_path = Path(config_path)
        self.make_radar = make_radar
        self.desktop = desktop or Desktop()
        self.opcodes = opcodes
        self.price_of = price_of
        self.item_name = item_name
        self.zonemaps = zonemaps
        self.zone_name = zone_name or (lambda z: z)
        self.notify = notify
        self.ocr = ocr_windows
        self.vision: dict | None = None
        self.heat = heat
        self.my_orders = my_orders
        self.session: Session | None = None
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.RLock()
        self.input_lock = threading.Lock()
        self.windows: dict[int, GameWindow] = {}
        self.feeds: dict[int, ClientFeed] = {}       # локальный порт → разбор
        self.names: dict[int, str] = {}              # pid → последний известный персонаж
        self.bot = Bot(self)
        self.recorder: Recorder | None = None
        self.message = ""
        self.config = self._load()
        self._grids: dict[str, Grid | None] = {}
        self._router: Router | None = None
        self._loop: threading.Thread | None = None
        self._loop_stop = threading.Event()
        self._refreshed = -1e18

    # --- настройки ------------------------------------------------------
    def _load(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULTS and isinstance(v, dict):
                        cfg[k].update({kk: vv for kk, vv in v.items() if kk in DEFAULTS[k]})
                    elif k in DEFAULT_CONFIG and isinstance(DEFAULT_CONFIG[k], dict) == isinstance(v, dict):
                        cfg[k] = v
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

    def task_config(self, name: str) -> dict:
        with self.lock:
            return {**DEFAULTS[name], **(self.config.get(name) or {})}

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
        session = self.session
        if session is not None:
            win = self.windows.get(self.bot.pid)
            if win is not None and local_port in win.ports:
                session.packet(local_port, payload)

    # --- запись сессий --------------------------------------------------
    def open_session(self, task: str) -> None:
        if not self.config.get("record") or self.session is not None:
            return
        try:
            self.session = Session(self.config_path.parent / "bot_sessions", task, self.clock)
            log.info("Запись сессии бота: %s", self.session.dir)
        except OSError as e:
            log.warning("Не удалось начать запись сессии бота: %s", e)

    def close_session(self) -> None:
        session, self.session = self.session, None
        if session is not None:
            session.close()

    def record(self, entry: dict) -> None:
        if self.session is not None:
            self.session.log(entry)

    def overlay(self) -> dict | None:
        """Что делает бот — для карты радара: зона, цель, путь, разведанные клетки."""
        if not self.bot.running:
            return None
        return {**self.bot.plan, "status": self.bot.status, "task": self.bot.task}

    def feed_for(self, pid: int | None) -> ClientFeed | None:
        win = self.windows.get(pid) if pid is not None else None
        if win is None:
            return None
        feeds = [self.feeds[p] for p in win.ports if p in self.feeds]
        if not feeds:
            return None
        return max(feeds, key=lambda f: (bool(f.character), f.last_packet_at))

    def character_of(self, pid: int | None) -> str:
        feed = self.feed_for(pid)
        name = feed.character if feed else ""
        if name and pid is not None:
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
            if self.bot.pid is not None and self.bot.pid not in self.windows:
                self.bot.stop()
        self._refreshed = self.clock()

    def target(self) -> int:
        """Окно игры для бота: активное сейчас, иначе то, с которым бот работал,
        иначе то, где последним шёл трафик."""
        if not self.windows:
            raise BotError("окно игры не найдено — запустите игру")
        fg = self.desktop.foreground()
        for pid, w in self.windows.items():
            if w.hwnd == fg:
                return pid
        if self.bot.pid in self.windows:
            return self.bot.pid

        def last_traffic(pid: int) -> float:
            feed = self.feed_for(pid)
            return feed.last_packet_at if feed else 0.0
        return max(self.windows, key=lambda p: (last_traffic(p), -p))

    # --- навигация ------------------------------------------------------
    def router(self) -> Router:
        if self._router is None:
            if self.zonemaps is None:
                raise BotError("нет списка зон — маршруты недоступны")
            try:
                self._router = Router(self.zonemaps.index())
            except (OSError, ValueError) as e:
                raise BotError(f"не удалось загрузить список зон: {e} (python -m albion_trader update-maps)") from None
        return self._router

    def zone_grid(self, zone: str) -> Grid | None:
        if not zone or self.zonemaps is None:
            return None
        if zone not in self._grids:
            try:
                data = self.zonemaps.get(zone, background=False)
                self._grids[zone] = Grid(data) if data.get("status") == "ready" and data.get("tiles") else None
            except Exception:  # noqa: BLE001 - нет схемы — идём напрямую
                log.debug("Нет схемы зоны %s", zone, exc_info=True)
                self._grids[zone] = None
        return self._grids[zone]

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
                background = cfg.get("input") == "background"
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
        self.bot.stop()

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
        if stop_vk and self.bot.running and self.desktop.key_down(stop_vk):
            self.bot.stop()
            self.message = f"бот остановлен клавишей {str(self.config.get('stop_key', 'f12')).upper()}"
            log.info(self.message)
        rec = self.recorder
        if rec is not None:
            win = self.windows.get(rec.pid)
            rec.poll(self.desktop, win)
            if rec.captured is not None and win is not None:
                with self.lock:
                    self.config["points"][rec.point] = list(rec.captured)
                    self.config["points_size"] = size_key(win)
                    self.save()
                self.message = f"точка «{POINTS[rec.point]}» запомнена"
                self.recorder = None

    # --- команды из интерфейса ------------------------------------------
    ACTIONS = {"settings", "configure", "start", "stop", "calibrate", "test_click", "save_place", "delete_place",
               "capture_point", "cancel_capture", "delete_point", "save_macro", "delete_macro",
               "record_start", "record_stop", "save_profile", "load_profile", "delete_profile", "clear_history",
               "snapshot", "apply_points", "probe_point"}

    def command(self, body: dict) -> dict:
        action = body.get("action")
        if action not in self.ACTIONS:
            raise BotError(f"неизвестная команда «{action}»")
        self.message = ""
        return getattr(self, "_cmd_" + action)(body) or {"ok": True}

    # --- автоопределение по снимку --------------------------------------
    def _cmd_snapshot(self, body: dict) -> dict:
        """Снимок окна игры, распознавание надписей, найденные точки интерфейса."""
        if self.bot.running:
            raise BotError("остановите бота, чтобы сделать снимок")
        if self.clock() - self._refreshed > 2.0:
            self.refresh()
        pid = self.target()
        win = self.windows[pid]
        with self.input_lock:
            prev = self.desktop.foreground()
            if not self.desktop.focus(win.hwnd):
                raise BotError("Windows не дала показать окно игры для снимка")
            self.desktop.sleep(0.4)            # окно успело перерисоваться поверх остальных
            try:
                w, h, pixels = self.desktop.capture(win)
            except OSError as e:
                raise BotError(f"не удалось снять окно игры: {e}") from None
            finally:
                if prev and prev != win.hwnd and self.config.get("restore_focus"):
                    self.desktop.focus(prev)
        vision = {"size": f"{w}x{h}", "image": data_url(png_encode(w, h, pixels)), "found": [], "error": "",
                  "lang": "", "lines": 0}
        try:
            scale = 2 if w <= 2000 else 1
            sw, sh, spx = scale2x(w, h, pixels) if scale == 2 else (w, h, pixels)
            ocr = self.ocr(png_encode(sw, sh, spx), str(body.get("lang") or ""))
            vision["found"] = find_points(ocr["lines"], w, h, scale)
            vision["lang"], vision["lines"] = ocr.get("lang", ""), len(ocr["lines"])
        except OcrError as e:
            vision["error"] = str(e)
        self.vision = vision
        found = ", ".join(POINTS[f["name"]] for f in vision["found"]) or "ничего"
        self.message = f"снимок {w}×{h}: найдено — {found}" if not vision["error"] else f"снимок сделан; {vision['error']}"
        return {"ok": True, "found": vision["found"], "error": vision["error"]}

    def _cmd_apply_points(self, body: dict) -> None:
        points = body.get("points") or {}
        if not isinstance(points, dict) or not points:
            raise BotError("нет точек для применения")
        clean = {}
        for name, v in points.items():
            if name not in POINTS:
                raise BotError(f"неизвестная точка «{name}»")
            try:
                x, y = float(v[0]), float(v[1])
            except (TypeError, ValueError, IndexError):
                raise BotError(f"точка «{name}»: нужны две доли окна") from None
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise BotError(f"точка «{name}»: координаты — доли окна от 0 до 1")
            clean[name] = [round(x, 4), round(y, 4)]
        with self.lock:
            self.config["points"].update(clean)
            size = (self.vision or {}).get("size")
            if size:
                self.config["points_size"] = size
            self.save()
        self.message = "применено: " + ", ".join(POINTS[n] for n in clean)

    def _cmd_probe_point(self, body: dict) -> None:
        name = body.get("name")
        if name not in PROBE_POINTS:
            raise BotError("пробными кликами ищутся только торговец рынка и сундук")
        pid = self._need_window()
        self.bot.probe_name = name
        self.bot.start("probe", pid)

    def _cmd_settings(self, body: dict) -> None:
        with self.lock:
            for k in SETTINGS:
                if k not in body:
                    continue
                v = body[k]
                if k == "stop_key":
                    v = str(v).lower()
                    if v not in VK:
                        raise BotError(f"неизвестная клавиша «{v}»")
                elif k == "user_idle":
                    v = max(0.0, min(60.0, float(v)))
                elif k in ("work_min", "rest_min"):
                    v = max(0.0, min(24 * 60.0, float(v)))
                elif k == "watchdog_min":
                    v = max(0.0, min(120.0, float(v)))
                elif k == "record":
                    v = bool(v)
                elif k == "schedule":
                    v = str(v or "")[:5000]
                    try:
                        parse_schedule(v, TASKS.keys() - {"schedule"})
                    except ValueError as e:
                        raise BotError(str(e)) from None
                elif k == "input" and v not in ("focus", "background"):
                    raise BotError("режим ввода: focus или background")
                elif k == "task" and v not in TASKS:
                    raise BotError("неизвестная задача")
                self.config[k] = v
            self.save()
        if self.config["enabled"]:
            self.start_loop()
            self.refresh()
        else:
            self.bot.stop()

    def _cmd_configure(self, body: dict) -> dict:
        with self.lock:
            for name in DEFAULTS:
                values = body.get(name)
                if isinstance(values, dict):
                    self.config[name].update({k: v for k, v in values.items() if k in DEFAULTS[name]})
            self.save()
            return {"ok": True, "config": {name: self.task_config(name) for name in DEFAULTS}}

    def _need_window(self) -> int:
        if self.clock() - self._refreshed > 2.0:
            self.refresh()
        pid = self.target()
        if not self.character_of(pid):
            raise BotError("персонаж в окне игры ещё не известен — смените зону, чтобы бот увидел вход")
        return pid

    def _cmd_start(self, body: dict) -> None:
        task = body.get("task") or self.config.get("task") or "gather"
        if task not in TASKS:
            raise BotError(f"неизвестная задача «{task}»")
        with self.lock:
            self.config["task"] = task
            enabling = not self.config.get("enabled")
            self.config["enabled"] = True        # «Запустить» сразу включает бота
            self.save()
        if enabling:
            self.start_loop()
            self.refresh()
        try:
            pid = self._need_window()
        except BotError as e:
            if enabling and "ещё не известен" in str(e):
                raise BotError("бот включён и слушает игру — смените зону в игре (выйдите из здания или "
                               "пройдите в соседнюю зону), затем нажмите «Запустить» ещё раз") from None
            raise
        missing = [c["item"] + (f" — {c['hint']}" if c.get("hint") else "")
                   for c in self.checklist(task) if c["required"] and not c["ok"]]
        if missing:
            raise BotError("не готово к запуску: " + "; ".join(missing))
        self.bot.start(task, pid)

    # --- профили, история ---------------------------------------------------
    def _cmd_save_profile(self, body: dict) -> None:
        name = (body.get("name") or "").strip()
        if not name or len(name) > 60:
            raise BotError("укажите имя профиля")
        with self.lock:
            self.config["profiles"][name] = json.loads(json.dumps({k: self.config.get(k) for k in PROFILE_KEYS}))
            self.save()
        self.message = f"профиль «{name}» сохранён"

    def _cmd_load_profile(self, body: dict) -> None:
        prof = self.config["profiles"].get(body.get("name") or "")
        if prof is None:
            raise BotError("нет такого профиля")
        if self.bot.running:
            raise BotError("остановите бота, чтобы сменить профиль")
        with self.lock:
            for k, v in prof.items():
                if k in DEFAULTS and isinstance(v, dict):
                    self.config[k] = {**DEFAULTS[k], **{kk: vv for kk, vv in v.items() if kk in DEFAULTS[k]}}
                elif k in PROFILE_KEYS:
                    self.config[k] = v
            self.save()
        self.message = f"профиль «{body.get('name')}» загружен"

    def _cmd_delete_profile(self, body: dict) -> None:
        with self.lock:
            self.config["profiles"].pop(body.get("name") or "", None)
            self.save()

    def _cmd_clear_history(self, _body: dict) -> None:
        with self.lock:
            self.config["history"] = []
            self.save()

    def add_history(self, entry: dict) -> None:
        with self.lock:
            self.config["history"] = (self.config.get("history") or [])[-(MAX_HISTORY - 1):] + [entry]
            self.save()

    def window_of(self, character: str) -> int | None:
        if self.clock() - self._refreshed > 2.0:
            self.refresh()
        for pid in self.windows:
            if self.character_of(pid).lower() == character.lower():
                return pid
        return None

    # --- чек-лист перед запуском -------------------------------------------
    def checklist(self, task: str) -> list[dict]:
        """Что нужно для задачи: [{item, ok, hint, required}]. Обязательные пункты блокируют запуск."""
        cfg = self.config
        points = cfg["points"]
        macros = cfg["macros"]
        places = cfg["places"]
        out: list[dict] = []

        def add(item, ok, hint="", required=True):
            out.append({"item": item, "ok": bool(ok), "hint": "" if ok else hint, "required": required})

        def place(name, title):
            if name:
                add(f"{title}: место «{name}»", name in places, "место удалено — сохраните его заново")

        def macro(name, title):
            if name:
                add(f"{title}: макрос «{name}»", name in macros, "макрос удалён — запишите его заново")

        def need_points(names, title):
            missing = [POINTS[n] for n in names if n not in points]
            add(title, not missing, "укажите точки: " + ", ".join(missing))

        add("Бот включён", cfg.get("enabled"), "галочка «включить бота»")
        game = self.windows
        add("Окно игры найдено", bool(game), "запустите игру")
        if game:
            try:
                pid = self.bot.pid if self.bot.running else self.target()
            except BotError:
                pid = None
            feed = self.feed_for(pid)
            add("Персонаж известен", bool(self.character_of(pid)), "смените зону в игре")
            # Работающий бот сам следит за трафиком (сторож), перезапуск задачи не блокируем.
            fresh = bool(feed and self.clock() - feed.last_packet_at < 60)
            add("Трафик игры идёт", fresh or (self.bot.running and self.bot.pid == pid),
                "игра свёрнута или не тот порт сервера (--game-port)")
            win = self.windows.get(pid)
            calibrated = bool(win and (cfg["calib"].get(size_key(win)) or {}).get("measured"))
            add("Калибровка для этого размера окна", calibrated, "сделается сама при запуске", required=False)
            if cfg["points"] and cfg.get("points_size") and win and cfg["points_size"] != size_key(win):
                add("Точки интерфейса для этого размера окна", False,
                    f"указаны в окне {cfg['points_size']}, сейчас {size_key(win)} — укажите заново", required=False)
        if task in ("gather", "wander"):
            g = self.task_config("gather")
            if int(g.get("bag_slots") or 0):
                add("Сбор: куда сдавать при полной сумке", g.get("home_place"), "выберите место")
                place(g.get("home_place"), "Сбор")
                if not g.get("deposit_macro"):
                    need_points(("stash_open", "stash_deposit"), "Сбор: точки сундука")
                macro(g.get("deposit_macro"), "Сбор")
        elif task == "market":
            m = self.task_config("market")
            add("Рынок: список предметов", split_items(m.get("items")), "задайте предметы")
            place(m.get("place"), "Рынок")
            if m.get("macro"):
                macro(m.get("macro"), "Рынок")
            else:
                side = "sell" if m.get("side", "sell") == "sell" else "buy"
                need_points(("market_npc", f"{side}_tab", "search", "first_item", f"{side}_order", "price", "qty",
                             "confirm"), "Рынок: точки интерфейса")
        elif task == "transport":
            t = self.task_config("transport")
            add("Перевозка: место погрузки", t.get("load_place"), "выберите место")
            add("Перевозка: место разгрузки", t.get("unload_place"), "выберите место")
            place(t.get("load_place"), "Перевозка")
            place(t.get("unload_place"), "Перевозка")
            macro(t.get("load_macro"), "Погрузка")
            if t.get("unload") == "market_sell":
                add("Перевозка: что продавать", split_items(t.get("sell_items")), "задайте предметы")
                need_points(("market_npc", "sell_tab", "search", "first_item", "sell_order", "price", "qty",
                             "confirm"), "Перевозка: точки рынка")
            else:
                macro(t.get("unload_macro"), "Разгрузка")
        elif task in ("dungeon", "dungeon_run"):
            d = self.task_config("dungeon")
            try:
                parse_skills(d.get("skills"))
                ok_skills = True
            except ValueError:
                ok_skills = False
            add("Данж: умения", ok_skills, "исправьте строку умений")
            if d.get("loot_bags") or d.get("open_chests"):
                need_points(("loot_all",), "Данж: кнопка «Взять всё»")
            add("Данж: клавиша быстрого выхода", d.get("exit_key"), "без неё — выход пешком по этажам",
                required=False)
            if task == "dungeon_run":
                add("Данжи: сундук в городе", d.get("home_place"), "выберите место")
                place(d.get("home_place"), "Данжи")
                if d.get("deposit_macro"):
                    macro(d.get("deposit_macro"), "Сдача добычи")
                else:
                    need_points(("stash_open", "stash_deposit"), "Данжи: точки сундука")
                for kind, title in (("repair", "Ремонт"), ("restock", "Докупка")):
                    if int(d.get(f"{kind}_every") or 0):
                        add(f"{title}: макрос", d.get(f"{kind}_macro"), "выберите макрос")
                        macro(d.get(f"{kind}_macro"), title)
                        place(d.get(f"{kind}_place"), title)
        elif task == "schedule":
            try:
                windows = parse_schedule(cfg.get("schedule") or "", TASKS.keys() - {"schedule"})
                add("Расписание", windows, "расписание пустое")
            except ValueError as e:
                add("Расписание", False, str(e))
        return out

    def _cmd_calibrate(self, _body: dict) -> None:
        self.bot.start("calibrate", self._need_window())

    def _cmd_stop(self, _body: dict) -> None:
        self.bot.stop()

    def _cmd_test_click(self, body: dict) -> None:
        pid = self._need_window()
        fx, fy = float(body.get("x", 0.5)), float(body.get("y", 0.62))
        threading.Thread(target=self._test_click, args=(pid, fx, fy), daemon=True).start()

    def _test_click(self, pid: int, fx: float, fy: float) -> None:
        b = self.bot
        if b.running:
            b.note("пробный клик не сделан: бот работает")
            return
        b.pid = pid
        b.stop_event.clear()
        try:
            before = b.feed.requests
            b.click_at(fx, fy)
            b.wait(1.0)
            b.note("пробный клик: игра " + ("ответила запросом серверу" if b.feed.requests != before
                                             else "не отправила запрос (клик не дошёл или мимо)"))
        except (BotError, BotStopped) as e:
            b.note(f"пробный клик не удался: {e}")

    def _cmd_save_place(self, body: dict) -> None:
        name = (body.get("name") or "").strip()
        if not name or len(name) > 60:
            raise BotError("укажите название места")
        feed = self.feed_for(self._need_window())
        me = feed.me
        if not me.get("zone"):
            raise BotError("зона неизвестна — смените зону в игре")
        with self.lock:
            self.config["places"][name] = {"zone": me["zone"], "x": round(me["x"], 1), "y": round(me["y"], 1)}
            self.save()
        self.message = f"место «{name}» запомнено: {self.zone_name(me['zone'])} ({me['x']:.0f}, {me['y']:.0f})"

    def _cmd_delete_place(self, body: dict) -> None:
        with self.lock:
            self.config["places"].pop(body.get("name") or "", None)
            self.save()

    def _cmd_capture_point(self, body: dict) -> None:
        name = body.get("name")
        if name not in POINTS:
            raise BotError("неизвестная точка")
        if self.clock() - self._refreshed > 2.0:
            self.refresh()
        self.recorder = Recorder(self.target(), self.clock, point=name)

    def _cmd_cancel_capture(self, _body: dict) -> None:
        self.recorder = None

    def _cmd_delete_point(self, body: dict) -> None:
        with self.lock:
            self.config["points"].pop(body.get("name") or "", None)
            self.save()

    def _cmd_save_macro(self, body: dict) -> None:
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

    def _cmd_delete_macro(self, body: dict) -> None:
        with self.lock:
            self.config["macros"].pop(body.get("name") or "", None)
            self.save()

    def _cmd_record_start(self, _body: dict) -> None:
        if self.clock() - self._refreshed > 2.0:
            self.refresh()
        self.recorder = Recorder(self.target(), self.clock)

    def _cmd_record_stop(self, _body: dict) -> dict:
        rec, self.recorder = self.recorder, None
        return {"ok": True, "text": rec.text() if rec and not rec.point else ""}

    # --- состояние для интерфейса ---------------------------------------
    def snapshot(self) -> dict:
        if self.config.get("enabled") and self.clock() - self._refreshed > 2.0:
            self.refresh()
        game = None
        if self.windows:
            pid = self.bot.pid if self.bot.running else self.target()
            feed = self.feed_for(pid)
            win = self.windows.get(pid)
            me = feed.me if feed else {}
            ents = feed.entities() if feed else []
            zone = me.get("zone", "")
            game = {"pid": pid, "windows": len(self.windows), "size": size_key(win) if win else "",
                    "character": self.character_of(pid),
                    "traffic": bool(feed and self.clock() - feed.last_packet_at < 30),
                    "zone": zone, "zone_name": self.zone_name(zone) if zone else "",
                    "pos": [round(me.get("x", 0), 1), round(me.get("y", 0), 1)] if feed else None,
                    "hp": round(feed.hp_pct) if feed and feed.hp_pct is not None else None,
                    "counts": {k: sum(1 for e in ents if e.kind == k) for k in ("resource", "mob", "loot", "player")},
                    "calibrated": bool(win and (self.config["calib"].get(size_key(win)) or {}).get("measured"))}
        rec = self.recorder
        cfg = self.config
        return {"supported": self.desktop.available, "enabled": bool(cfg.get("enabled")),
                "settings": {k: cfg.get(k) for k in SETTINGS},
                "game": game, "bot": self.bot.snapshot(),
                "tasks_config": {name: self.task_config(name) for name in DEFAULTS},
                "places": [{"name": n, **p, "zone_name": self.zone_name(p["zone"])}
                           for n, p in sorted(cfg["places"].items())],
                "points": [{"name": n, "label": label, "value": cfg["points"].get(n)} for n, label in POINTS.items()],
                "points_size": cfg.get("points_size", ""),
                "templates": TEMPLATES, "macros": cfg["macros"],
                "recording": {"point": rec.point, "text": rec.text()} if rec else None,
                "message": self.message, "tasks": TASKS, "safety": SAFETY_NAMES, "portal_kinds": PORTAL_KINDS,
                "checklist": self.checklist(cfg.get("task") or "gather"),
                "profiles": sorted(cfg.get("profiles") or {}), "history": list(reversed(cfg.get("history") or [])),
                "schedule_help": SCHEDULE_HELP, "skills_help": SKILLS_HELP,
                "auto_points": {"text": list(LABELS), "probe": list(PROBE_POINTS)},
                "macro_help": MACRO_HELP, "keys": sorted(VK)}
