"""Проверка ботов на этом компьютере (``check-bots``): функции Windows, права,
окна игры и их порты, работающая программа (персонажи, трафик, калибровка,
макросы) и — по желанию — переключение на окно игры и обратно.

Отчёт пишется в ``data/bots_check.txt``; ``check_bots.bat`` запускает всё сразу.
"""

from __future__ import annotations

from pathlib import Path

from . import radar_check
from .bot_win import Desktop
from .bots import parse_macro
from .selfcheck import FAIL, INFO, OK, WARN, Report

USER32 = ("EnumWindows", "GetWindowThreadProcessId", "SetForegroundWindow", "SetCursorPos", "SendInput",
          "PostMessageW", "GetLastInputInfo", "GetAsyncKeyState", "GetClientRect", "ClientToScreen")


def check_support(r: Report, desktop: Desktop) -> bool:
    r.section("Windows для ботов")
    if not desktop.available:
        r.add(INFO, "Боты работают только в Windows — на этом компьютере проверяются только настройки программы")
        return False
    try:
        u = desktop.api.user32
        missing = [n for n in USER32 if not hasattr(u, n)]
        if not hasattr(desktop.api.iphlpapi, "GetExtendedUdpTable"):
            missing.append("GetExtendedUdpTable")
    except (OSError, AttributeError) as e:
        r.add(FAIL, f"Библиотеки Windows не загрузились: {e}")
        return False
    r.check(not missing, "Функции Windows для кликов, клавиш и поиска окон доступны",
            "Нет функций Windows: " + ", ".join(missing))
    admin = radar_check.is_admin()
    if admin is None:
        r.add(WARN, "Не удалось узнать, есть ли права администратора")
    else:
        r.check(admin, "Права администратора есть",
                "Нет прав администратора: клики не дойдут до игры, запущенной от администратора, "
                "и трафик не будет виден — запускайте программу от администратора", bad=WARN)
    return not missing


def check_windows(r: Report, desktop: Desktop) -> list:
    r.section("Окна игры")
    wins = desktop.game_windows()
    if not wins:
        r.add(WARN, "Окна игры не найдены — запустите клиент(ы) Albion Online и повторите проверку")
        return []
    r.add(OK, f"Найдено окон игры: {len(wins)}")
    for w in wins:
        size = f"{w.rect[2]}×{w.rect[3]}"
        r.check(bool(w.ports), f"Окно {w.pid} ({w.exe or w.title}), {size}, UDP-порты: {', '.join(map(str, w.ports))}",
                f"Окно {w.pid} ({w.exe or w.title}), {size}: нет UDP-портов — игра ещё не подключилась к серверу?",
                bad=WARN)
        if w.rect[2] < 800 or w.rect[3] < 500:
            r.add(WARN, f"Окно {w.pid} маленькое ({size}) — интерфейс игры может закрывать точки кликов")
    if len({(w.rect[2], w.rect[3]) for w in wins}) > 1:
        r.add(INFO, "Окна разного размера: макросы рынка пишутся в долях окна, но кнопки игры при другом "
                    "размере могут оказаться в другом месте — записывайте макрос под каждый размер")
    return wins


def check_live(r: Report, url: str, fetch=radar_check._json) -> bool:
    r.section("Программа и бот")
    try:
        d = fetch(url + "/api/bots")
    except Exception as e:  # noqa: BLE001 - любая ошибка = программа недоступна
        r.add(WARN, f"Программа не отвечает на {url} ({e}) — запустите Albion Trader и повторите")
        return False
    if not d.get("enabled"):
        r.add(WARN, "Бот выключен — включите его во вкладке «Боты», затем смените зону в игре")
    else:
        r.add(OK, "Бот включён")
    g = d.get("game")
    if not g:
        r.add(WARN, "Окно игры не найдено программой")
    else:
        who = g.get("character") or "персонаж"
        if not g.get("character"):
            r.add(WARN, "Персонаж неизвестен — смените зону в игре, чтобы бот увидел вход")
        else:
            r.check(bool(g.get("traffic")), f"{who}: трафик идёт, зона {g.get('zone_name') or g.get('zone') or '—'}",
                    f"{who}: нет трафика последние 30 с — окно свёрнуто, игра на паузе или порт сервера не тот "
                    "(--game-port)", bad=WARN)
        r.check(bool(g.get("calibrated")), f"Калибровка для окна {g.get('size')} есть",
                f"Нет калибровки для окна {g.get('size')} — нажмите «Калибровка» на открытом месте", bad=WARN)
        if g.get("windows", 1) > 1:
            r.add(INFO, f"Окон игры: {g['windows']} — бот работает с тем, что активно при запуске")
    bot = d.get("bot") or {}
    if bot.get("status") == "ошибка" and bot.get("log"):
        r.add(WARN, f"Последняя ошибка бота — {bot['log'][-1]['text']}")
    places = d.get("places") or []
    r.add(INFO, "Сохранённые места: " + (", ".join(f"{p['name']} ({p.get('zone_name') or p['zone']})"
                                                    for p in places) or "нет"))
    points = {p["name"]: p for p in d.get("points") or []}
    missing = [p["label"] for p in points.values() if not p.get("value")]
    if not missing:
        r.add(OK, "Все точки интерфейса указаны")
    else:
        r.add(INFO, f"Не указано точек интерфейса: {len(missing)} из {len(points)} (нужны для рынка и добычи)")
    size = g.get("size") if g else ""
    if d.get("points_size") and size and d["points_size"] != size:
        r.add(WARN, f"Точки интерфейса указывались в окне {d['points_size']}, а сейчас окно {size} — "
                    "укажите их заново")
    values = {n: p["value"] for n, p in points.items() if p.get("value")}
    for name, text in sorted((d.get("macros") or {}).items()):
        try:
            steps = parse_macro(text, values)
            r.add(OK, f"Макрос «{name}»: шагов {len(steps)}"
                      + ("" if any(s[0] == "expect" for s in steps) else " (без expect — ошибки не будут замечены)"))
        except ValueError as e:
            r.add(FAIL, f"Макрос «{name}» с ошибкой: {e}")
    return True


def check_focus(r: Report, desktop: Desktop, wins: list) -> None:
    """Переключиться на первое окно игры и вернуться — проверка, что Windows разрешает ботам это."""
    r.section("Переключение на окно игры")
    if not wins:
        r.add(INFO, "Нет окон игры — пропущено")
        return
    prev = desktop.foreground()
    ok = desktop.focus(wins[0].hwnd)
    r.check(ok, f"Окно игры {wins[0].pid} стало активным",
            f"Windows не дала сделать окно игры {wins[0].pid} активным — запустите программу от администратора "
            "или выберите в боте ввод «без переключения»")
    if prev and prev != wins[0].hwnd:
        r.check(desktop.focus(prev), "Прежнее окно снова активно", "Не удалось вернуть прежнее окно", bad=WARN)


def run(data_dir: str | Path, url: str = "http://127.0.0.1:8484", out: str | Path | None = None,
        try_focus: bool = False, desktop: Desktop | None = None, fetch=radar_check._json) -> tuple[str, Path]:
    desktop = desktop or Desktop()
    r = Report()
    wins: list = []
    try:
        if check_support(r, desktop):
            wins = check_windows(r, desktop)
    except Exception as e:  # pragma: no cover - проверка не должна падать целиком
        r.add(FAIL, f"Проверка Windows упала: {e!r}")
    try:
        check_live(r, url.rstrip("/"), fetch)
    except Exception as e:  # pragma: no cover
        r.add(FAIL, f"Проверка программы упала: {e!r}")
    if try_focus and desktop.available:
        try:
            check_focus(r, desktop, wins)
        except Exception as e:  # pragma: no cover
            r.add(FAIL, f"Проверка переключения упала: {e!r}")
    text = r.text().replace("самопроверка", "проверка ботов", 1)
    out = Path(out) if out else Path(data_dir) / "bots_check.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text, out
