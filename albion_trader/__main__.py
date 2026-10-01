"""Точка входа: ``python -m albion_trader <команда>``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import secrets
import sys
from pathlib import Path

from . import db
from .gamedata import download as download_gamedata
from .items import download_catalog
from .capture.opcodes import update as update_opcodes
from .capture.sniffer import Sniffer, read_pcap
from .server import App, AppConfig, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="albion_trader",
        description="Локальный анализатор рынка Albion Online со встроенным сборщиком данных.")
    parser.add_argument("--data-dir", default=os.environ.get("ALBION_TRADER_DATA", "data"),
                        help="каталог для базы и справочников (по умолчанию ./data)")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="запустить приёмник данных и веб-интерфейс")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="адрес (по умолчанию 127.0.0.1 — доступно только с этого ПК)")
    p_serve.add_argument("--port", type=int, default=8484)
    p_serve.add_argument("--token", default=os.environ.get("ALBION_TRADER_TOKEN", ""),
                         help="секрет в пути приёма данных; 'auto' — сгенерировать")
    p_serve.add_argument("--retention-hours", type=float, default=72,
                         help="сколько хранить не обновлявшиеся заказы (по умолчанию 72 ч)")
    p_serve.add_argument("--no-capture", action="store_true",
                         help="не запускать встроенный сборщик (данные только по HTTP от внешнего клиента)")
    p_serve.add_argument("--password", default=os.environ.get("ALBION_TRADER_PASSWORD", ""),
                         help="пароль для доступа к интерфейсу из сети (обязателен с --host 0.0.0.0)")
    p_serve.add_argument("--record", metavar="FILE.pcap",
                         help="записывать трафик игры в файл для диагностики "
                              "(содержит и ваш чат/ник — не выкладывайте публично)")
    p_serve.add_argument("--game-port", type=int, action="append", dest="game_ports", metavar="PORT",
                         help="UDP-порт игрового сервера (по умолчанию 5056; для своего сервера — его порт; "
                              "можно несколько раз). Также переменная ALBION_TRADER_GAME_PORTS=5056,5055")
    p_serve.add_argument("--tray", action="store_true", help="значок в трее Windows")
    p_serve.add_argument("--open-browser", action="store_true", help="открыть интерфейс в браузере")
    p_serve.add_argument("--window", action="store_true",
                         help="открыть окно-компаньон с инструментами (отдельное окно без вкладок)")
    p_serve.add_argument("--fetch-reference", action="store_true",
                         help="скачать справочники, если их нет (первый запуск)")
    p_serve.add_argument("--log-file", action="store_true", help="писать лог в data/albion_trader.log")
    p_serve.add_argument("-v", "--verbose", action="store_true")

    p_replay = sub.add_parser("replay", help="загрузить данные из записи трафика (.pcap)")
    p_replay.add_argument("pcap", help="файл .pcap (например, из Wireshark)")

    sub.add_parser("update-opcodes", help="обновить номера операций игры из albiondata-client (после патчей)")

    sub.add_parser("update-items", help="скачать названия предметов (RU/EN) и игровые таблицы из ao-bin-dumps")

    p_maps = sub.add_parser("update-maps", help="скачать схемы зон для фона радара (из ao-bin-dumps)")
    p_maps.add_argument("zones", nargs="*", help="id зон (например, 3004 0201); по умолчанию — города и зоны, "
                                                  "где вы бывали")
    p_maps.add_argument("--all", action="store_true", help="все зоны игры (долго: сотни мегабайт)")

    p_check = sub.add_parser("check", help="самопроверка всех вкладок на ваших данных (отчёт в data/check_report.txt)")
    p_check.add_argument("--url", default="http://127.0.0.1:8484", help="адрес работающей программы (если запущена)")
    p_check.add_argument("--out", help="куда записать отчёт (по умолчанию data/check_report.txt)")

    p_rc = sub.add_parser("check-radar", help="проверка радара на этом компьютере (отчёт в data/radar_check.txt)")
    p_rc.add_argument("--url", default="http://127.0.0.1:8484", help="адрес работающей программы")
    p_rc.add_argument("--out", help="куда записать отчёт (по умолчанию data/radar_check.txt)")
    p_rc.add_argument("--window", action="store_true",
                      help="открыть окно радара и проверить «поверх игры» и оверлей (программа должна работать)")
    p_rc.add_argument("--no-browser", action="store_true", help="не открывать страницу самотеста в браузере")

    p_clean = sub.add_parser("cleanup", help="удалить истёкшие и устаревшие заказы")
    p_clean.add_argument("--retention-hours", type=float, default=72)
    return parser


def game_ports(cli: list[int] | None, env: str | None = None) -> tuple[int, ...]:
    """Порты игрового сервера: из --game-port, иначе из ALBION_TRADER_GAME_PORTS, иначе 5056."""
    env = os.environ.get("ALBION_TRADER_GAME_PORTS", "") if env is None else env
    ports = list(cli or []) or [int(p) for p in env.replace(";", ",").split(",") if p.strip().isdigit()]
    ports = [p for p in ports if 0 < p < 65536]
    return tuple(dict.fromkeys(ports)) or (5056,)


def _safe_console() -> None:
    """Старая консоль Windows (cp1252 и т. п.) не умеет кириллицу: печать падала с
    UnicodeEncodeError. Недопустимые символы заменяем, а не роняем команду."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):   # поток уже закрыт или не текстовый
                pass


def main(argv=None) -> int:
    _safe_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    db_path = data_dir / "market.db"
    items_path = data_dir / "items.json"
    opcodes_path = data_dir / "opcodes.json"

    cmd = args.cmd or "serve"
    if cmd == "serve":
        verbose = getattr(args, "verbose", False)
        log_kwargs = {}
        # У .exe без консоли нет stderr — пишем лог в файл (с ротацией: 5 МБ × 3 файла).
        if getattr(args, "log_file", False) or sys.stderr is None:
            data_dir.mkdir(parents=True, exist_ok=True)
            log_kwargs["handlers"] = [logging.handlers.RotatingFileHandler(
                data_dir / "albion_trader.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")]
        logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S", **log_kwargs)
        token = getattr(args, "token", "")
        if token == "auto":
            token_file = data_dir / "token.txt"
            if token_file.exists():
                token = token_file.read_text().strip()
            else:
                token = secrets.token_urlsafe(16)
                data_dir.mkdir(parents=True, exist_ok=True)
                token_file.write_text(token)
        config = AppConfig(db_path=db_path, items_path=items_path, token=token,
                           retention_hours=getattr(args, "retention_hours", 72),
                           capture=not getattr(args, "no_capture", False),
                           opcodes_path=opcodes_path,
                           record_path=getattr(args, "record", None),
                           password=getattr(args, "password", ""),
                           tray=getattr(args, "tray", False),
                           open_browser=getattr(args, "open_browser", False),
                           open_window=getattr(args, "window", False),
                           fetch_reference=getattr(args, "fetch_reference", False),
                           game_ports=game_ports(getattr(args, "game_ports", None)))
        serve(config, getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8484))
        return 0
    if cmd == "update-items":
        print("Скачиваю справочник предметов…")
        n = download_catalog(items_path)
        print(f"Готово: {n} предметов -> {items_path}")
        print("Скачиваю рецепты и игровые таблицы…")
        stats = download_gamedata(data_dir / "gamedata.json")
        print(f"Готово: рецептов {stats['recipes']}, дневников {stats['journals']}, "
              f"растений {stats['plants']}, животных {stats['animals']}")
        return 0
    if cmd == "update-opcodes":
        res = update_opcodes(opcodes_path)
        print(f"Готово: {opcodes_path}")
        if res["missing"]:
            print("Не найдены в исходниках:", ", ".join(res["missing"]))
        return 0
    if cmd == "replay":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        app = App(AppConfig(db_path=db_path, items_path=items_path, capture=False,
                            opcodes_path=opcodes_path))
        sniffer = app.sniffer = Sniffer(app.albion)
        for packet in read_pcap(args.pcap):
            sniffer.feed_ip_packet(packet)
        st = app.capture_status()
        print(f"Пакетов Albion: {st['packets']}, пакетов заказов: {st['order_batches']} "
              f"({st['orders']} заказов), историй: {st['history_batches']}, "
              f"последняя локация: {st['location'] or 'не определена'}")
        unanswered = len(app.albion.pending_market_requests) + st["market_responses_lost"]
        print(f"Запросов рынка: {st['market_requests']}, без ответа: {unanswered}, "
              f"дублей пакетов отброшено: {st['duplicates']}, "
              f"страниц без известной локации: {st['no_location_drops']}")
        if st["encrypted_at"]:
            print("Внимание: данные рынка в записи зашифрованы.")
        return 0
    if cmd == "update-maps":
        from .zonemaps import ZoneMaps
        from .gamedata import GameData
        logging.basicConfig(level=logging.WARNING)
        gd = GameData.load(data_dir / "gamedata.json")
        zm = ZoneMaps(data_dir / "zonemaps", name_of=gd.cluster_name)
        index = zm.index()
        ids = list(args.zones)
        if args.all:
            ids = sorted(index)
        elif not ids:
            ids = sorted(cid for cid, c in index.items() if c["type"].startswith("PLAYERCITY"))
            if db_path.exists():
                conn = db.connect(db_path)
                try:
                    ids += [r[0] for r in conn.execute(
                        "SELECT DISTINCT target FROM activity_events WHERE kind='zone' AND target IS NOT NULL")]
                except Exception:   # таблиц ещё нет
                    pass
                finally:
                    conn.close()
            ids = [i for i in dict.fromkeys(ids) if i in index]
        res = zm.prefetch(ids, lambda cid, n, total, status: print(f"[{n}/{total}] {cid} {zm.zone_name(cid)}: {status}"))
        print(f"Готово: {res['ok']} из {res['total']}, ошибок {res['failed']} -> {zm.dir}")
        return 0
    if cmd == "check":
        from .selfcheck import run as run_check
        logging.basicConfig(level=logging.ERROR)
        text, out = run_check(data_dir, args.url, args.out)
        if sys.stdout is not None:
            try:
                print(text)
            except UnicodeEncodeError:   # старая консоль Windows без UTF-8
                print(text.encode("ascii", "replace").decode())
            print(f"Отчёт сохранён: {out}")
        if getattr(sys, "frozen", False) and hasattr(os, "startfile"):
            os.startfile(out)            # у .exe нет консоли — открываем отчёт в Блокноте
        return 0
    if cmd == "check-radar":
        from .radar_check import run as run_radar_check
        logging.basicConfig(level=logging.ERROR)
        text, out = run_radar_check(data_dir, args.url, args.out, try_window=args.window)
        if sys.stdout is not None:
            try:
                print(text)
            except UnicodeEncodeError:   # старая консоль Windows без UTF-8
                print(text.encode("ascii", "replace").decode())
            print(f"Отчёт сохранён: {out}")
        if not args.no_browser and "Программа не отвечает" not in text:
            import webbrowser
            webbrowser.open(args.url.rstrip("/") + "/radar-selftest.html")
        if getattr(sys, "frozen", False) and hasattr(os, "startfile"):
            os.startfile(out)
        return 1 if "[FAIL]" in text else 0
    if cmd == "cleanup":
        db.init_db(db_path)
        conn = db.connect(db_path)
        try:
            with conn:
                print(db.cleanup(conn, args.retention_hours))
        finally:
            conn.close()
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
