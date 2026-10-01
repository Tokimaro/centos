"""Самопроверка на реальных данных: каждая вкладка прогоняется на вашей базе.

Команда ``python -m albion_trader check`` (или ``AlbionTrader.exe check``).
Работает на КОПИИ базы — ваши данные не меняются. Если программа запущена,
дополнительно проверяется работающий сервер (сборщик, ответы API).

Отчёт пишется в ``data/check_report.txt`` — его можно прислать разработчику.
В отчёт не попадают имена персонажей, гильдий и игроков, токены и пароли:
только числа, идентификаторы предметов и зон.
"""

from __future__ import annotations

import json
import math
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from . import __version__

OK, WARN, FAIL, INFO, SKIP = "OK", "WARN", "FAIL", "INFO", "SKIP"
MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[info]", SKIP: "[skip]"}
TOL = 2.0          # допуск округления при сверке сумм


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.counts = Counter()
        self.section_name = ""

    def section(self, name: str) -> None:
        self.section_name = name
        self.lines += ["", "=" * 70, name, "=" * 70]

    def add(self, status: str, text: str) -> None:
        self.counts[status] += 1
        self.lines.append(f"{MARK[status]} {text}")

    def check(self, cond: bool, ok_text: str, bad_text: str | None = None, bad: str = FAIL) -> bool:
        self.add(OK if cond else bad, ok_text if cond else (bad_text or ok_text))
        return cond

    def text(self) -> str:
        head = [f"Albion Trader {__version__} — самопроверка {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Итог: OK {self.counts[OK]}, WARN {self.counts[WARN]}, FAIL {self.counts[FAIL]}, "
                f"SKIP {self.counts[SKIP]}"]
        return "\n".join(head + self.lines) + "\n"


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}".replace(",", " ")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)


def _close(a, b, tol: float = TOL) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _get(url: str, timeout: float = 30):
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r), _ms(t0)


# --- подготовка -------------------------------------------------------------------

def copy_data(data_dir: Path, tmp: Path) -> Path:
    """Копия базы (через backup — корректно и при работающем сервере) и справочников."""
    src = data_dir / "market.db"
    dst = tmp / "market.db"
    if src.exists():
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
    for name in ("items.json", "gamedata.json", "opcodes.json"):
        if (data_dir / name).exists():
            shutil.copy2(data_dir / name, tmp / name)
    return dst


def last_session(conn) -> int | None:
    row = conn.execute("SELECT session_id FROM activity_events WHERE session_id IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1").fetchone()
    return row[0] if row else None


# --- проверки ----------------------------------------------------------------------

def check_environment(r: Report, app, data_dir: Path) -> None:
    r.section("1. Среда и справочники")
    r.add(INFO, f"Python {platform.python_version()}, {platform.system()} {platform.release()}, "
                f"{'exe' if getattr(sys, 'frozen', False) else 'исходники'}")
    db_file = data_dir / "market.db"
    r.add(INFO, f"База: {_fmt(db_file.stat().st_size // 1024) if db_file.exists() else 0} КБ")
    g = app.gamedata
    r.check(len(app.catalog) > 1000, f"Названия предметов: {_fmt(len(app.catalog))}",
            f"Названий предметов мало: {len(app.catalog)} — выполните update-items", WARN)
    r.check(g.version >= 2, f"gamedata.json версии {g.version}",
            f"gamedata.json версии {g.version} — старый, обновится при запуске или update-items")
    r.check(len(g.recipes) > 1000, f"Рецептов: {_fmt(len(g.recipes))}", f"Рецептов мало: {len(g.recipes)}")
    with_slot = sum(1 for m in g.items.values() if m.get("slot"))
    with_ip = sum(1 for m in g.items.values() if m.get("ip"))
    r.check(with_slot > 1000 and with_ip > 1000, f"Предметов со слотом: {_fmt(with_slot)}, с силой: {_fmt(with_ip)}",
            f"Мало предметов со слотом/силой ({with_slot}/{with_ip}) — конструктор билдов будет пуст")
    nodes = g.destiny.get("nodes", {})
    r.check(len(nodes) > 300, f"Узлов Доски судьбы: {len(nodes)}, шаблонов: {len(g.destiny.get('templates', {}))}",
            f"Узлов Доски судьбы: {len(nodes)} — нет achievements.json в справочнике")
    roads = sum(1 for c in g.clusters.values() if c[1].startswith("TUNNEL"))
    r.check(len(g.clusters) > 500, f"Зон на карте: {len(g.clusters)}, из них Дорог Авалона: {roads}",
            f"Зон на карте: {len(g.clusters)} — нет cluster/world.json в справочнике")
    with app.conn() as c:
        for row in c.execute("SELECT topic, last_at, records FROM ingest_stats ORDER BY last_at DESC"):
            r.add(INFO, f"Данные {row['topic']}: записей {_fmt(row['records'])}, "
                        f"последние {_fmt(int(time.time()) - row['last_at'])} с назад")
        orders = c.execute("SELECT COUNT(*), COUNT(DISTINCT item_id) FROM orders").fetchone()
        r.add(INFO, f"Заказов в базе: {_fmt(orders[0])}, предметов: {_fmt(orders[1])}")


def check_live(r: Report, url: str) -> bool:
    r.section("2. Работающая программа (сборщик)")
    try:
        st, ms = _get(url + "/api/status", 5)
    except OSError as e:
        r.add(SKIP, f"Программа не отвечает на {url} ({e.__class__.__name__}) — проверки сборщика пропущены. "
                    f"Запустите программу и повторите, чтобы проверить и её.")
        return False
    c = st.get("capture") or {}
    r.add(INFO, f"/api/status ответил за {ms} мс")
    if not r.check(bool(c.get("enabled")), "Сборщик включён", "Сборщик выключен (--no-capture?)", WARN):
        return True
    r.check(bool(c.get("running")) and not c.get("error"), "Сборщик работает",
            f"Сборщик не работает: {c.get('error')}")
    r.add(INFO, f"Пакетов игры: {_fmt(c.get('packets'))}, событий разобрано: {_fmt(c.get('events'))}, "
                f"дублей отброшено: {_fmt(c.get('duplicates'))}, незавершённых сообщений: {_fmt(c.get('incomplete_messages'))}")
    r.check(bool(c.get("zone")), f"Текущая зона: {c.get('zone')} ({c.get('zone_name')})",
            "Текущая зона не определена — смените зону в игре", WARN)
    r.check(bool(c.get("location")), f"Рыночная локация: {c.get('location')} ({c.get('location_name')})",
            "Рыночная локация не определена — зайдите в город", WARN)
    r.check(bool(c.get("character")), "Персонаж определён (имя в отчёт не пишется)",
            "Персонаж не определён — смените зону в игре", WARN)
    if c.get("market_requests"):
        lost = c.get("market_responses_lost") or 0
        r.check(lost <= c["market_requests"] * 0.1, f"Запросов рынка: {c['market_requests']}, потеряно ответов: {lost}",
                f"Много потерянных ответов рынка: {lost} из {c['market_requests']}", WARN)
    if c.get("encrypted_at"):
        r.add(WARN, f"Данные рынка приходили зашифрованными ({_fmt(st['now'] - c['encrypted_at'])} с назад)")
    if c.get("orders_by_content"):
        r.add(WARN, f"Заказы опознаны по содержимому {c['orders_by_content']} раз — номера операций могли сместиться "
                    f"после патча (кнопка «Обновить коды» во вкладке «Статус»)")
    return True


def check_raw_events(r: Report, app) -> None:
    r.section("3. Сырые события за 7 дней (проверка разбора протокола)")
    since = int(time.time()) - 7 * 86400
    with app.conn() as c:
        kinds = dict(c.execute("SELECT kind, COUNT(*) FROM activity_events WHERE ts >= ? GROUP BY kind", (since,)).fetchall())
        r.add(INFO, "Событий по видам: " + (", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "нет"))
        if not kinds:
            r.add(WARN, "За 7 дней нет событий активности — играйте с запущенным сборщиком и повторите")
            return
        fame = [json.loads(x[0] or "{}") | {"v": x[1]} for x in
                c.execute("SELECT data, value FROM activity_events WHERE kind='fame' AND ts >= ?", (since,))]
        if fame:
            srcs = Counter(f.get("src", "нет (старая запись)") for f in fame)
            r.add(INFO, f"Слава: {len(fame)} событий, источники: " + ", ".join(f"{k} {v}" for k, v in srcs.most_common()))
            prem = sum(1 for f in fame if f.get("premium"))
            sat = sum(1 for f in fame if f.get("satchel"))
            bonus = [f.get("bonus") for f in fame if f.get("bonus")]
            r.add(INFO, f"Слава с премиумом: {prem}, с сумкой прозрения: {sat}, с бонус-фактором: {len(bonus)}"
                        + (f" (от {min(bonus)} до {max(bonus)})" if bonus else ""))
            bad = [f for f in fame if not math.isfinite(f["v"] or 0) or (f["v"] or 0) < 0 or (f["v"] or 0) > 10 ** 8]
            r.check(not bad, "Значения славы правдоподобны (0…100 млн за событие)",
                    f"Подозрительных значений славы: {len(bad)}")
            parts_bad = [f for f in fame if "base" in f and not _close(
                (f["base"] + f.get("premium", 0) + f.get("satchel", 0)) * (1 + f.get("bonus", 0)), f["v"], 1)]
            r.check(not parts_bad, "Слава = (база + премиум + сумка) × (1 + бонус) во всех событиях",
                    f"Формула не сходится в {len(parts_bad)} событиях")
        silver = [(json.loads(x[0] or "{}"), x[1], x[2]) for x in
                  c.execute("SELECT data, value, amount FROM activity_events WHERE kind='silver' AND ts >= ?", (since,))]
        if silver:
            gross = sum(a or 0 for _, _, a in silver)
            ctax = sum(d.get("cluster_tax", 0) for d, _, _ in silver)
            gtax = sum(d.get("guild_tax", 0) for d, _, _ in silver)
            with_data = sum(1 for d, _, _ in silver if d)
            r.add(INFO, f"Серебро с мобов: {len(silver)} событий, до налогов {_fmt(round(gross))}, "
                        f"налог кластера {_fmt(round(ctax))}, гильдии {_fmt(round(gtax))}, с разбивкой налогов: {with_data}")
            if gross:
                share = (ctax + gtax) / gross
                r.check(share < 0.6, f"Доля налогов {share:.1%} — правдоподобно",
                        f"Доля налогов {share:.1%} — подозрительно много (индексы параметров?)", WARN)
        chests = [json.loads(x[0] or "{}").get("rarity") for x in
                  c.execute("SELECT data FROM activity_events WHERE kind='chest' AND ts >= ?", (since,))]
        if chests:
            dist = Counter("неизвестно" if x is None else str(x) for x in chests)
            r.add(INFO, "Сундуки по редкости (0 обычный … 3 легендарный): " + ", ".join(f"{k}: {v}" for k, v in sorted(dist.items())))
            r.check(dist.get("неизвестно", 0) < len(chests) * 0.5, "Редкость сундуков определяется",
                    "У большинства сундуков редкость неизвестна — индекс параметра редкости, вероятно, сместился", WARN)
        else:
            r.add(INFO, "Открытых сундуков за 7 дней нет")
        balances = kinds.get("balance", 0)
        r.check(balances > 0, f"Отметок баланса: {balances}", "Нет отметок баланса — «прочее» во вкладке «Учёт» не посчитать", WARN)


def check_now(r: Report, app, sid: int | None) -> None:
    r.section("4. Вкладка «Сейчас»")
    if not sid:
        r.add(SKIP, "Нет сессий с событиями")
        return
    t0 = time.perf_counter()
    rep = app.api_session({"id": str(sid)})["report"]
    r.add(INFO, f"Последняя сессия #{sid}: {rep.get('hours')} ч, слава {_fmt(rep.get('fame'))} "
                f"({_fmt(rep.get('fame_per_hour'))}/ч), серебро {_fmt(rep.get('silver'))}, "
                f"лут {_fmt(rep.get('loot_value'))} — за {_ms(t0)} мс")
    r.check(all(k in rep for k in ("fame_per_hour", "silver_per_hour", "loot_value_per_hour", "hours")),
            "Все поля карточек на месте", "Не хватает полей для карточек")


def _check_chain_node(node: dict, errors: list, path: str = "") -> int:
    """Сверка узла дерева; возвращает глубину."""
    here = f"{path}/{node['item_id']}"
    if node["decision"] == "craft" and node.get("craft_total") is not None:
        kids = sum(c["best_total"] or 0 for c in node["children"])
        expect = kids + (node.get("station_fee") or 0) + (node.get("silver") or 0)
        if not _close(expect, node["craft_total"], max(TOL, len(node["children"]) * 0.02 + 1)):
            errors.append(f"{here}: сделать {node['craft_total']} ≠ сумма ресурсов {round(expect, 2)}")
    if node["decision"] == "buy" and node.get("craft_total") is not None and node["buy_total"] > node["craft_total"] + TOL:
        errors.append(f"{here}: выбрано «купить», хотя сделать дешевле")
    if node.get("return_rate") is not None and not 0 <= node["return_rate"] < 70:
        errors.append(f"{here}: возврат {node['return_rate']}% вне 0…70")
    if node["qty"] <= 0:
        errors.append(f"{here}: количество {node['qty']}")
    return 1 + max((_check_chain_node(c, errors, here) for c in node["children"]), default=0)


def check_chain(r: Report, app) -> None:
    r.section("5. Вкладка «Цепочка»")
    g = app.gamedata
    if not g:
        r.add(SKIP, "Нет рецептов")
        return
    with app.conn() as c:
        popular = [row[0] for row in c.execute(
            "SELECT item_id FROM orders GROUP BY item_id ORDER BY COUNT(*) DESC LIMIT 400")]
    items = [i for i in popular if (g.recipes.get(i) or {}).get("kind") in ("craft", "refine")][:6]
    if not items:
        items = ["T4_BAG", "T5_PLANKS"]
        r.add(WARN, "В базе нет цен на крафтовые предметы — проверяю на T4_BAG и T5_PLANKS (цен может не хватить)")
    markets = "thetford,lymhurst,bridgewatch,martlock,fort_sterling,caerleon,brecilien"
    for item in items:
        t0 = time.perf_counter()
        res = app.api_chain({"item": item, "qty": "10", "buy_markets": markets, "craft_city": "martlock",
                             "refine_city": "martlock", "sell_market": "martlock", "max_age": "48"})
        ms = _ms(t0)
        errors: list[str] = []
        depth = _check_chain_node(res["tree"], errors)
        r.add(INFO, f"{item}: себестоимость {_fmt(res['cost'])}, выручка {_fmt(res['revenue'])}, прибыль {_fmt(res['profit'])}, "
                    f"глубина {depth}, без цены: {len(res['missing'])}, покупок: {len(res['shopping'])}, {ms} мс")
        r.check(not errors, f"{item}: суммы и решения в дереве сходятся", f"{item}: " + "; ".join(errors[:5]))
        r.check(res["cost"] is not None or bool(res["missing"]),
                f"{item}: " + ("себестоимость посчитана" if res["cost"] is not None else "названы ресурсы без цены"),
                f"{item}: себестоимости нет, а список «нет цен» пуст")
        if res["cost"] is not None:
            shop = sum(s["total"] for s in res["shopping"])
            r.check(shop <= res["cost"] + TOL, f"{item}: список покупок ({_fmt(round(shop))}) не больше себестоимости",
                    f"{item}: список покупок {round(shop)} больше себестоимости {res['cost']}")
        r.check(ms < 3000, f"{item}: расчёт быстрый", f"{item}: расчёт медленный ({ms} мс)", WARN)


def check_build(r: Report, app) -> None:
    r.section("6. Вкладка «Билд»")
    g = app.gamedata
    slots = ["mainhand", "head", "armor", "shoes", "cape", "bag", "mount", "food", "potion"]
    build = {"name": "самопроверка", "slots": {}}
    with app.conn() as c:
        for item, n in c.execute("SELECT item_id, COUNT(*) FROM orders WHERE auction_type='offer' "
                                 "GROUP BY item_id ORDER BY COUNT(*) DESC LIMIT 3000"):
            slot = (g.items.get(item) or {}).get("slot")
            if slot in slots and slot not in build["slots"]:
                build["slots"][slot] = {"item": item, "quality": 1}
        saved = c.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
    r.add(INFO, f"Сохранённых билдов: {saved}")
    if not build["slots"]:
        r.add(SKIP, "Нет предложений на экипировку в базе — откройте на рынке несколько предметов")
        return
    main = build["slots"].get("mainhand")
    if main and (g.items.get(main["item"]) or {}).get("2h"):
        build["slots"].pop("offhand", None)
    r.add(INFO, "Тестовый билд из самых продаваемых предметов: " +
          ", ".join(f"{s}={v['item']}" for s, v in build["slots"].items()))
    t0 = time.perf_counter()
    res = app.api_build_price({}, {"build": build, "max_age": 72})
    ms = _ms(t0)
    best = sum(row["best_price"] for row in res["rows"] if row["best_price"] is not None)
    r.check(_close(best, res["cheapest_total"]), f"«Дешевле всего» = сумма лучших цен по слотам ({_fmt(best)}), {ms} мс",
            f"«Дешевле всего» {res['cheapest_total']} ≠ сумма по слотам {best}")
    bad_markets = []
    for m in res["markets"]:
        s = sum(row["markets"][m["market"]]["price"] for row in res["rows"] if m["market"] in row["markets"])
        if not _close(s, m["total"]):
            bad_markets.append(m["market"])
    r.check(not bad_markets, "Суммы по городам сходятся", f"Суммы не сходятся: {bad_markets}")
    ip = res["average_ip"]
    if ip is None:
        r.add(INFO, "Средняя сила не считается — в тестовом билде нет оружия и брони")
    else:
        r.check(100 <= ip <= 2500, f"Средняя сила {_fmt(ip)} — правдоподобно", f"Средняя сила {ip} — странно")
    if main:
        row = next((x for x in res["rows"] if x["slot"] == "mainhand"), None)
        r.check(bool(row and row.get("spec_name")), f"У оружия виден узел Доски судьбы: {row and row.get('spec_name')}",
                "У оружия нет узла Доски судьбы", WARN)
    complete = [m for m in res["markets"] if m["complete"]]
    r.add(INFO, f"Городов, где есть весь билд: {len(complete)} из {len(res['markets'])}")


def check_meta(r: Report, app, url: str | None) -> None:
    r.section("7. Вкладка «Мета» (киллборд)")
    s = app.settings()
    r.add(INFO, f"Загрузка киллборда {'включена' if s.get('killboard_enabled') else 'выключена'}, сервер: {s.get('killboard_region')}")
    if url:
        try:
            live, _ = _get(url + "/api/killboard?hours=24", 30)
            st = live.get("status") or {}
            if st.get("error"):
                r.add(FAIL, f"Ошибка загрузки киллборда: {st['error']}")
            elif st.get("last_fetch_at"):
                r.add(OK, f"Последняя загрузка {_fmt(live['now'] - st['last_fetch_at'])} с назад, новых событий: {st.get('last_new')}")
            elif s.get("killboard_enabled"):
                r.add(WARN, "Загрузка включена, но ещё не выполнялась — подождите 1–2 минуты или нажмите «Обновить сейчас»")
        except OSError:
            pass
    with app.conn() as c:
        per = c.execute("SELECT region, COUNT(*), MAX(ts) FROM kb_events GROUP BY region").fetchall()
    if not per:
        r.add(SKIP, "Событий киллборда нет — включите загрузку во вкладке «Мета» и повторите через 5–10 минут")
        return
    for region, n, latest in per:
        r.add(INFO, f"Сервер {region}: событий {_fmt(n)}, последнее {_fmt(int(time.time()) - latest)} с назад")
    res = app.api_killboard({"hours": "168", "region": per[0][0]})
    r.add(INFO, f"Боёв в выборке: {res['events']}, билдов: {len(res['builds'])}, предметов в «спросе»: {len(res['demand'])}")
    bad = [b for b in res["builds"] if b["kills"] + b["deaths"] != b["total"] or not 0 <= b["win_rate"] <= 100]
    r.check(not bad, "Убийства + смерти = бои, доля побед 0…100%", f"Несходящихся билдов: {len(bad)}")
    unnamed = [b for b in res["builds"] if not b["names"].get("mainhand") or b["names"]["mainhand"] == b["signature"]["mainhand"]]
    r.check(len(unnamed) <= len(res["builds"]) * 0.2, "Названия оружия находятся", f"Без названия: {len(unnamed)} билдов", WARN)
    priced = sum(1 for d in res["demand"] if d["price"] is not None)
    r.add(INFO, f"Предметов «спроса» с ценой: {priced} из {len(res['demand'])}")


def check_destiny(r: Report, app) -> None:
    r.section("8. Вкладка «Доска»")
    from . import destiny
    g = app.gamedata
    nodes = g.destiny.get("nodes", {})
    if not nodes:
        r.add(SKIP, "Нет данных Доски судьбы")
        return
    titles = Counter(destiny.node_title(g, app.catalog.name, n) for n in nodes)
    dups = [(t, k) for t, k in titles.items() if k > 1]
    r.check(len(dups) <= 5, f"Названия узлов различимы (повторов: {len(dups)})",
            "Повторяющиеся названия: " + "; ".join(f"{t} ×{k}" for t, k in dups[:8]), WARN)
    spec = next((n for n, v in nodes.items() if v["tpl"] == "COMBAT_SPEC" and v.get("mult") == 1), None)
    if spec:
        total = sum(destiny.level_table(g, spec))
        r.check(30e6 < total < 40e6, f"Специализация оружия до 100: {_fmt(round(total))} славы (ожидается ~34 млн)",
                f"Специализация оружия до 100: {round(total)} — не похоже на ~34 млн", WARN)
    res = app.api_destiny({"days": "7"})
    r.add(INFO, f"Время в игре за 7 дней: {res['hours']} ч, слава в час: " +
          ", ".join(f"{k} {_fmt(v)}" for k, v in res["rates"].items() if v))
    r.add(INFO, f"Отслеживаемых узлов: {len(res['rows'])}")
    for row in res["rows"]:
        ok = 0 <= row["level"] <= 100 and row["remaining"] >= 0
        r.check(ok, f"{row['node_id']}: уровень {row['level']} → {row['target']}, осталось {_fmt(row['remaining'])}, "
                    f"набрано {_fmt(row['gained'])}, прогноз {_fmt(row['eta_hours'])} ч",
                f"{row['node_id']}: странные значения {row}")


def check_avalon(r: Report, app) -> None:
    r.section("9. Вкладка «Авалон»")
    from . import avalon
    g = app.gamedata
    if not g.clusters:
        r.add(SKIP, "Нет карты мира")
        return
    res = app.api_avalon({})
    auto = sum(1 for l in res["links"] if l["source"] == "auto")
    timed = sum(1 for l in res["links"] if l["expires"])
    r.add(INFO, f"Связей: {len(res['links'])} (замечено при переходе: {auto}, с указанным временем: {timed})")
    route = avalon.route(g, [], "3004", "4000", True)       # Мартлок → Форт Стерлинг по карте
    r.check(route is not None, f"Маршрут Мартлок → Форт Стерлинг по карте мира: {len(route['steps']) if route else '—'} переходов",
            "Маршрут между городами по карте мира не найден — переходы карты не разобрались")
    since = int(time.time()) - 30 * 86400
    with app.conn() as c:
        zones = [row[0] for row in c.execute("SELECT DISTINCT target FROM activity_events WHERE kind='zone' AND ts >= ?", (since,))]
    roads = [z for z in zones if avalon.is_road(g, z)]
    r.add(INFO, f"Зон за 30 дней: {len(zones)}, из них Дорог Авалона: {len(roads)}" + (f" ({', '.join(roads[:10])})" if roads else ""))
    if roads and not auto:
        r.add(WARN, "Были на Дорогах, но связей не замечено — возможно, переход через портал не распознаётся")


def check_dungeons(r: Report, app) -> None:
    r.section("10. Вкладка «Данжи»")
    from . import dungeons
    g = app.gamedata
    res = app.api_dungeons({"days": "30"})
    runs = res["runs"]
    r.add(INFO, f"Прохождений за 30 дней: {len(runs)} — " +
          ", ".join(f"{s['name']} {s['runs']}" for s in res["summary"]))
    bad = [x for x in runs if x["seconds"] < 0 or not _close(x["income"], x["silver"] + x["loot_silver"] + x["loot_value"], 3)]
    r.check(not bad, "Время и доход прохождений сходятся", f"Несходящихся прохождений: {len(bad)}")
    long = [x for x in runs if x["seconds"] > 4 * 3600]
    r.check(not long, "Нет прохождений длиннее 4 часов", f"Прохождений длиннее 4 ч: {len(long)} — выход из данжа мог не распознаться", WARN)
    since = int(time.time()) - 30 * 86400
    with app.conn() as c:
        zones = Counter(row[0] for row in c.execute("SELECT target FROM activity_events WHERE kind='zone' AND ts >= ?", (since,)))
    kinds = Counter()
    unknown = []
    for z, n in zones.items():
        k = dungeons.dungeon_kind(g, z)
        kinds[k or "обычная зона"] += n
        if k == "instance":
            unknown.append(z)
    r.add(INFO, "Входов в зоны по типам: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    if unknown:
        r.add(INFO, "Зоны, опознанные как «случайный данж / инстанс» (пришлите — уточню распознавание): " + ", ".join(unknown[:30]))
    no_type = [z for z in zones if z and not g.cluster_type(z) and not dungeons.dungeon_kind(g, z)]
    if no_type:
        r.add(INFO, "Зоны без типа на карте (не считаются данжами): " + ", ".join(no_type[:30]))


def check_economy(r: Report, app, sid: int | None) -> None:
    r.section("11. Вкладка «Учёт»")
    for label, q in (("последняя сессия", {"period": "session", "session": str(sid or 0)}), ("7 дней", {"period": "7"})):
        t0 = time.perf_counter()
        res = app.api_economy(q)
        ms = _ms(t0)
        f = res["fame"]
        r.add(INFO, f"{label}: слава {_fmt(f['total'])}, доход {_fmt(res['income'])}, в игре {res['hours']} ч, {ms} мс")
        r.check(_close(sum(f["by_source"].values()), f["total"], 5), f"{label}: слава по источникам = итогу",
                f"{label}: по источникам {sum(f['by_source'].values())} ≠ итог {f['total']}")
        r.check(_close(f["base"] + f["premium"] + f["satchel"] + f["bonus"], f["total"], 5),
                f"{label}: база + премиум + сумка + бонусы = итогу",
                f"{label}: части славы не сходятся с итогом")
        r.check(_close(sum(h["fame"] for h in res["hourly"]), f["total"], 5), f"{label}: график славы сходится с итогом",
                f"{label}: график славы не сходится с итогом")
        s = res["silver"]
        r.check(s["mobs_net"] <= s["mobs_gross"] + TOL, f"{label}: серебро чистыми ≤ до налогов",
                f"{label}: чистыми {s['mobs_net']} > до налогов {s['mobs_gross']}", WARN)
        b = res["balance"]
        if b:
            r.add(INFO, f"{label}: баланс {_fmt(b['start'])} → {_fmt(b['end'])}, изменение {_fmt(b['change'])}, "
                        f"учтено {_fmt(b['explained'])}, прочее {_fmt(b['other'])}")
        else:
            r.add(WARN if label == "7 дней" else INFO, f"{label}: баланс не виден (нужно хотя бы две отметки)")


def check_http(r: Report, url: str) -> None:
    r.section("12. Ответы API работающей программы по вкладкам")
    paths = ["/api/session", "/api/chain?item=T4_BAG", "/api/builds", "/api/killboard", "/api/destiny",
             "/api/avalon", "/api/dungeons", "/api/economy", "/api/window", "/api/deals", "/api/fastsell",
             "/api/craft", "/api/zones", "/api/loot", "/api/world", "/api/radar", "/api/zonemap"]
    for p in paths:
        try:
            _, ms = _get(url + p, 60)
            r.check(ms < 5000, f"{p}: {ms} мс", f"{p}: медленно — {ms} мс", WARN)
        except urllib.error.HTTPError as e:
            r.add(FAIL, f"{p}: HTTP {e.code} {e.read()[:150]!r}")
        except OSError as e:
            r.add(FAIL, f"{p}: {e}")


def _radar_files(r: Report, data_dir: Path) -> None:
    from .radar_check import check_files
    check_files(r, data_dir)
    r.add(INFO, "Полная проверка радара (права, окно, оверлей, сеть): команда check-radar или check_radar.bat")


def run(data_dir: str | Path, url: str = "http://127.0.0.1:8484", out: str | Path | None = None) -> tuple[str, Path]:
    from .server import App, AppConfig
    data_dir = Path(data_dir)
    r = Report()
    live = False
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            db_copy = copy_data(data_dir, tmp)
            with sqlite3.connect(db_copy) as c:
                try:
                    sid = last_session(c)
                except sqlite3.OperationalError:
                    sid = None
            app = App(AppConfig(db_path=db_copy, items_path=tmp / "items.json", capture=False))
        except Exception as e:  # pragma: no cover - база повреждена и т. п.
            r.section("Ошибка подготовки")
            r.add(FAIL, f"Не удалось открыть копию базы: {e!r}")
            app = None
        if app:
            steps = [("environment", lambda: check_environment(r, app, data_dir)),
                     ("live", lambda: None),
                     ("raw", lambda: check_raw_events(r, app)),
                     ("now", lambda: check_now(r, app, sid)),
                     ("chain", lambda: check_chain(r, app)),
                     ("build", lambda: check_build(r, app)),
                     ("meta", lambda: check_meta(r, app, url if live else None)),
                     ("destiny", lambda: check_destiny(r, app)),
                     ("avalon", lambda: check_avalon(r, app)),
                     ("dungeons", lambda: check_dungeons(r, app)),
                     ("economy", lambda: check_economy(r, app, sid)),
                     ("radar", lambda: _radar_files(r, data_dir))]
            for name, fn in steps:
                if name == "live":
                    try:
                        live = check_live(r, url.rstrip("/"))
                    except Exception:
                        r.add(FAIL, "Проверка сборщика упала:\n" + traceback.format_exc(limit=4))
                    continue
                try:
                    fn()
                except Exception:
                    r.add(FAIL, f"Проверка «{name}» упала с ошибкой:\n" + traceback.format_exc(limit=6))
            if live:
                try:
                    check_http(r, url.rstrip("/"))
                except Exception:
                    r.add(FAIL, "Проверка API упала:\n" + traceback.format_exc(limit=4))
    text = r.text()
    out = Path(out) if out else data_dir / "check_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text, out
