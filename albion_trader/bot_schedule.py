"""Расписание бота: окна времени по дням недели, в каждом — своя задача и персонаж.

Строка расписания::

    пн-пт 08:00-12:00 gather
    * 18:00-23:30 dungeon_run Hero
    сб,вс 22:00-02:00 market Trader      # через полночь

Дни: ``*`` — каждый день, ``пн``…``вс`` (или ``mon``…``sun``), диапазоны через «-»,
списки через запятую. Персонаж (необязательно) — бот возьмёт окно с этим персонажем.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
SCHEDULE_HELP = """Строка — окно времени: дни, время, задача, персонаж (необязательно).
пн-пт 08:00-12:00 gather
* 18:00-23:30 dungeon_run Hero
сб,вс 22:00-02:00 market Trader     (через полночь — до 02:00 следующего дня)
Дни: * — каждый день, пн вт ср чт пт сб вс, диапазоны «пн-пт», списки «сб,вс».
Задачи: gather, market, transport, dungeon_run, dungeon, wander."""
_TIME = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


@dataclass
class Window:
    days: frozenset
    start: int              # минуты от полуночи
    end: int
    task: str
    character: str = ""

    def active(self, when: dt.datetime) -> bool:
        minute = when.hour * 60 + when.minute
        day = when.weekday()
        if self.start < self.end:
            return day in self.days and self.start <= minute < self.end
        # Через полночь: вечер этого дня или утро следующего.
        return (day in self.days and minute >= self.start) or ((day - 1) % 7 in self.days and minute < self.end)

    def ends_at(self, when: dt.datetime) -> dt.datetime:
        midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
        end = midnight + dt.timedelta(minutes=self.end)
        if end <= when:
            end += dt.timedelta(days=1)
        return end


def parse_days(text: str) -> frozenset:
    if text == "*":
        return frozenset(range(7))
    out = set()
    for part in text.lower().split(","):
        a, _, b = part.partition("-")
        if a not in DAYS or (b and b not in DAYS):
            raise ValueError(f"непонятные дни «{text}»")
        if not b:
            out.add(DAYS[a])
            continue
        i = DAYS[a]
        while True:
            out.add(i)
            if i == DAYS[b]:
                break
            i = (i + 1) % 7
    return frozenset(out)


def parse_schedule(text: str, tasks) -> list[Window]:
    out = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        try:
            if len(parts) < 3:
                raise ValueError("нужно: дни время задача")
            days = parse_days(parts[0])
            m = _TIME.match(parts[1])
            if not m:
                raise ValueError(f"время «{parts[1]}» — нужно ЧЧ:ММ-ЧЧ:ММ")
            h1, m1, h2, m2 = (int(v) for v in m.groups())
            if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
                raise ValueError(f"время «{parts[1]}» вне суток")
            start, end = h1 * 60 + m1, min(h2 * 60 + m2, 24 * 60)
            if start == end:
                raise ValueError("начало и конец совпадают")
            task = parts[2]
            if task not in tasks:
                raise ValueError(f"неизвестная задача «{task}»")
            out.append(Window(days, start, end, task, " ".join(parts[3:])))
        except ValueError as e:
            raise ValueError(f"расписание, строка {n}: {e}") from None
    return out


def current(windows: list[Window], when: dt.datetime) -> Window | None:
    return next((w for w in windows if w.active(when)), None)


def next_start(windows: list[Window], when: dt.datetime) -> dt.datetime | None:
    """Ближайшее начало окна в течение недели (для статуса «ждёт до …»)."""
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = []
    for day in range(8):
        for w in windows:
            t = midnight + dt.timedelta(days=day, minutes=w.start)
            if t > when and t.weekday() in w.days:
                starts.append(t)
    return min(starts) if starts else None
