"""Проверка радара на этом компьютере (в первую очередь — Windows).

``python -m albion_trader check-radar`` (или ``check_radar.bat``, ``AlbionTrader.exe check-radar``):

1. Окружение: система, Python, права администратора, открытие сокетов захвата.
2. Окно радара: найден ли браузер на Chromium, есть ли функции Windows для «поверх игры»
   и оверлея.
3. Сеть: ao-bin-dumps (схемы зон, мобы) и render.albiononline.com (значки — необязательно).
4. Файлы данных радара: список зон, схемы, мобы, номера событий, настройки, записи.
5. Работающая программа: события от игры, зона, схема зоны, история.
6. Окно радара на деле (с ``--window``): открыть, «поверх игры», оверлей и обратно.

Отчёт — ``data/radar_check.txt``; имён игроков и персонажей в нём нет.
"""

from __future__ import annotations

import json
import platform
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import window as window_mod
from .selfcheck import FAIL, INFO, OK, SKIP, WARN, Report, _ms

DUMPS_PROBE = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/cluster/world.json"
RENDER_PROBE = "https://render.albiononline.com/v1/item/T4_ORE.png?size=32"


def _json(url: str, data: dict | None = None, timeout: float = 10):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _reachable(url: str, timeout: float = 10) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except OSError as e:
        return False, e.__class__.__name__


def is_admin() -> bool | None:
    if sys.platform != "win32":
        try:
            import os
            return os.geteuid() == 0
        except AttributeError:
            return None
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return None


def check_environment(r: Report, open_sockets=None) -> None:
    r.section("1. Окружение")
    r.add(INFO, f"{platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}"
                + (", собранная программа (.exe)" if getattr(sys, "frozen", False) else ""))
    r.check(sys.version_info >= (3, 10), "Python 3.10+", f"Python {platform.python_version()} — нужен 3.10+")
    admin = is_admin()
    if admin is None:
        r.add(WARN, "Не удалось узнать, есть ли права администратора")
    else:
        r.check(admin, "Программа запущена с правами администратора",
                "Нет прав администратора — захват трафика игры не заработает (запустите от администратора)", WARN)
    if open_sockets is None:
        from .capture.sniffer import open_capture_sockets as open_sockets
    t0 = time.perf_counter()
    try:
        socks = open_sockets()
        for s in socks:
            s.close()
        r.add(OK, f"Сокеты захвата открываются ({len(socks)} шт., {_ms(t0)} мс)")
    except Exception as e:   # PermissionError, CaptureError, OSError
        r.add(WARN, f"Сокеты захвата не открылись: {e} — если программа уже работает и ловит пакеты, это не страшно "
                    f"(сокет занят ею); иначе нужны права администратора")


def check_window_support(r: Report) -> None:
    r.section("2. Окно радара: браузер и функции Windows")
    browser = window_mod.find_browser()
    r.check(bool(browser), f"Браузер для окна найден: {Path(browser).name if browser else ''}",
            "Браузер на Chromium (Edge, Chrome, Brave) не найден — окно радара откроется вкладкой браузера", WARN)
    if not window_mod.IS_WINDOWS:
        r.add(SKIP, "«Поверх игры» и оверлей работают только в Windows")
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        missing = [n for n in ("SetWindowPos", "GetWindowLongW", "SetWindowLongW", "SetLayeredWindowAttributes",
                               "EnumWindows", "GetWindowTextW") if not hasattr(user32, n)]
        r.check(not missing, "Функции Windows для «поверх игры» и оверлея доступны",
                f"Нет функций Windows: {', '.join(missing)}")
    except (OSError, AttributeError) as e:
        r.add(FAIL, f"user32.dll недоступна: {e}")


def check_network(r: Report, probe=_reachable) -> None:
    r.section("3. Сеть")
    ok, info = probe(DUMPS_PROBE)
    r.check(ok, f"ao-bin-dumps доступен ({info}) — схемы зон и справочник мобов скачаются",
            f"ao-bin-dumps недоступен ({info}) — схемы зон и мобы не скачаются (интернет, прокси, брандмауэр)", WARN)
    ok, info = probe(RENDER_PROBE)
    r.check(ok, f"render.albiononline.com доступен ({info}) — можно включить значки из игры",
            f"render.albiononline.com недоступен ({info}) — используйте свои значки (по умолчанию)", INFO)


def check_files(r: Report, data_dir: Path) -> None:
    r.section("4. Файлы данных радара")
    zm = data_dir / "zonemaps"
    idx = zm / "index.json"
    if idx.exists():
        try:
            n = len(json.loads(idx.read_text(encoding="utf-8")))
            r.check(n > 500, f"Список зон: {n}", f"Список зон подозрительно мал: {n}", WARN)
        except (OSError, ValueError) as e:
            r.add(FAIL, f"Список зон повреждён ({e}) — удалите {idx}, он скачается снова")
    else:
        r.add(INFO, "Списка зон ещё нет — скачается при первом открытии радара или командой update-items")
    maps = [p for p in zm.glob("*.json") if p.name != "index.json"] if zm.exists() else []
    bad = []
    for p in maps:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not d.get("tiles") and not d.get("exits_world"):
                bad.append(p.name)
        except (OSError, ValueError):
            bad.append(p.name)
    r.add(INFO, f"Скачанных схем зон: {len(maps)}")
    if bad:
        r.add(WARN, f"Повреждённые схемы (удалите, скачаются заново): {', '.join(bad[:10])}")
    mobs = data_dir / "mobs.json"
    if mobs.exists():
        try:
            r.check(len(json.loads(mobs.read_text(encoding="utf-8"))) > 1000, "Справочник мобов на месте",
                    "Справочник мобов неполный — удалите data/mobs.json", WARN)
        except (OSError, ValueError):
            r.add(FAIL, "Справочник мобов повреждён — удалите data/mobs.json")
    else:
        r.add(INFO, "Справочника мобов ещё нет — скачается при первой встрече с мобом")
    for name, what in (("opcodes.json", "номера событий"), ("radar.json", "настройки радара")):
        p = data_dir / name
        if not p.exists():
            r.add(INFO, f"{name} нет — используются {what} по умолчанию")
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            extra = f", своих номеров событий: {len(d.get('events') or {})}" if name == "opcodes.json" else ""
            r.add(OK, f"{name} читается{extra}")
        except (OSError, ValueError) as e:
            r.add(FAIL, f"{name} повреждён: {e}")
    maps_dir = data_dir / "maps"
    imgs = [p.name for p in maps_dir.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")] \
        if maps_dir.exists() else []
    r.add(INFO, f"Своих картинок карт: {len(imgs)}" + (f" ({', '.join(imgs[:5])})" if imgs else ""))
    r.add(INFO, f"Записей для перемотки (.pcap): {len(list(data_dir.glob('*.pcap')))}")


def check_live(r: Report, url: str) -> bool:
    r.section("5. Работающая программа: радар")
    try:
        t0 = time.perf_counter()
        d = _json(url + "/api/radar", timeout=5)
    except OSError as e:
        r.add(SKIP, f"Программа не отвечает на {url} ({e.__class__.__name__}) — запустите её и повторите проверку")
        return False
    r.check(_ms(t0) < 1500, f"/api/radar ответил за {_ms(t0)} мс", f"/api/radar медленный: {_ms(t0)} мс", WARN)
    codes = d.get("codes") or []
    if r.check(bool(codes), f"От игры пришли события: разных кодов {len(codes)}",
               "Событий от игры нет — запущена ли игра, есть ли права администратора у программы?", WARN):
        named = sum(1 for c in codes if c.get("name"))
        r.add(INFO, f"Из них известных радару: {named}")
        for s in d.get("suggestions") or []:
            r.add(WARN, f"Номер события «{s['name']}» на этом сервере, похоже, {s['code']} (настроен {s['current']}) — "
                        f"примените на вкладке «Радар» → «Коды событий»")
    me = d.get("me") or {}
    if r.check(bool(me.get("zone")), f"Зона: {me.get('zone')} ({me.get('zone_name')})",
               "Зона не определена — смените зону в игре", WARN):
        try:
            m = _json(url + "/api/zonemap?zone=" + urllib.request.quote(me["zone"]), timeout=10)
            for _ in range(20):
                if m.get("status") != "loading":
                    break
                time.sleep(1)
                m = _json(url + "/api/zonemap?zone=" + urllib.request.quote(me["zone"]), timeout=10)
            r.check(m.get("status") == "ready" or bool(m.get("image")),
                    f"Фон зоны готов ({len(m.get('tiles') or [])} тайлов{', своя картинка' if m.get('image') else ''})",
                    f"Фон зоны не готов: {m.get('error') or m.get('status')}", WARN)
        except OSError as e:
            r.add(WARN, f"/api/zonemap не ответил: {e}")
    kinds = {}
    for e in d.get("entities") or []:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    r.add(INFO, "На радаре: " + (", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "пусто"))
    r.add(INFO, f"Разбор движения сейчас {'включён' if d.get('moves') else 'выключен (включается, пока открыт радар)'}")
    try:
        h = _json(url + "/api/radar/history", timeout=5)
        r.add(OK, f"История встреч: {len(h.get('rows') or [])} игроков (имена в отчёт не пишутся)")
    except OSError as e:
        r.add(WARN, f"/api/radar/history не ответил: {e}")
    return True


def check_window_live(r: Report, url: str, pause: float = 3.0) -> None:
    """Открыть окно радара и проверить «поверх игры» и оверлей (только с --window)."""
    r.section("6. Окно радара на деле")
    try:
        w = _json(url + "/api/window?which=radar", timeout=5)
    except OSError as e:
        r.add(SKIP, f"Программа не отвечает ({e.__class__.__name__})")
        return
    try:
        res = _json(url + "/api/window", {"which": "radar", "action": "open"})
    except urllib.error.HTTPError as e:
        r.add(SKIP, f"Окно открывается только с компьютера, где запущена программа (HTTP {e.code})")
        return
    r.add(OK if res.get("mode") == "app" else WARN,
          "Окно радара открыто отдельным окном" if res.get("mode") == "app"
          else "Окно радара открыто вкладкой браузера (браузер на Chromium не найден)")
    time.sleep(pause)                                  # окну нужно время появиться
    if not w.get("windows"):
        r.add(SKIP, "«Поверх игры» и оверлей проверяются только в Windows")
        return
    top = _json(url + "/api/window", {"which": "radar", "topmost": True})
    r.check(bool(top.get("applied")), "«Поверх игры» применилось", f"«Поверх игры» не применилось: {top.get('reason')}")
    ov = _json(url + "/api/window", {"which": "radar", "overlay": True, "alpha": 70})
    r.check(bool(ov.get("overlay_applied")), "Оверлей включился (окно полупрозрачное и пропускает клики)",
            f"Оверлей не включился: {ov.get('reason')}")
    time.sleep(pause)
    off = _json(url + "/api/window", {"which": "radar", "overlay": False, "topmost": False})
    r.check(bool(off.get("overlay_applied")), "Оверлей выключен — окно снова принимает клики",
            "Оверлей не выключился — выключите его на вкладке «Радар»")


def run(data_dir: str | Path, url: str = "http://127.0.0.1:8484", out: str | Path | None = None,
        try_window: bool = False) -> tuple[str, Path]:
    data_dir = Path(data_dir)
    r = Report()
    steps = [lambda: check_environment(r), lambda: check_window_support(r), lambda: check_network(r),
             lambda: check_files(r, data_dir)]
    for step in steps:
        try:
            step()
        except Exception as e:  # pragma: no cover - проверка не должна падать целиком
            r.add(FAIL, f"Проверка упала: {e!r}")
    live = False
    try:
        live = check_live(r, url.rstrip("/"))
    except Exception as e:  # pragma: no cover
        r.add(FAIL, f"Проверка программы упала: {e!r}")
    if try_window and live:
        try:
            check_window_live(r, url.rstrip("/"))
        except Exception as e:  # pragma: no cover
            r.add(FAIL, f"Проверка окна упала: {e!r}")
    r.lines += ["", "Проверка интерфейса радара в браузере: " + url.rstrip("/") + "/radar-selftest.html"]
    text = r.text().replace("самопроверка", "проверка радара", 1)
    out = Path(out) if out else data_dir / "radar_check.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text, out
