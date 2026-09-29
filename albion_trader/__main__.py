"""Точка входа: ``python -m albion_trader <команда>``."""

from __future__ import annotations

import argparse
import logging
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


def main(argv=None) -> int:
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
    p_serve.add_argument("-v", "--verbose", action="store_true")

    p_replay = sub.add_parser("replay", help="загрузить данные из записи трафика (.pcap)")
    p_replay.add_argument("pcap", help="файл .pcap (например, из Wireshark)")

    sub.add_parser("update-opcodes", help="обновить номера операций игры из albiondata-client (после патчей)")

    sub.add_parser("update-items", help="скачать названия предметов (RU/EN) и игровые таблицы из ao-bin-dumps")

    p_clean = sub.add_parser("cleanup", help="удалить истёкшие и устаревшие заказы")
    p_clean.add_argument("--retention-hours", type=float, default=72)

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    db_path = data_dir / "market.db"
    items_path = data_dir / "items.json"
    opcodes_path = data_dir / "opcodes.json"

    cmd = args.cmd or "serve"
    if cmd == "serve":
        verbose = getattr(args, "verbose", False)
        logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
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
                           password=getattr(args, "password", ""))
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
