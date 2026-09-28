"""Точка входа: ``python -m albion_trader <команда>``."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path

from . import db
from .items import download_catalog
from .server import AppConfig, serve


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="albion_trader",
        description="Локальный анализатор рынка Albion Online (данные только от вашего клиента).")
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
    p_serve.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("update-items", help="скачать названия предметов (RU/EN) из ao-bin-dumps")

    p_clean = sub.add_parser("cleanup", help="удалить истёкшие и устаревшие заказы")
    p_clean.add_argument("--retention-hours", type=float, default=72)

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    db_path = data_dir / "market.db"
    items_path = data_dir / "items.json"

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
                           retention_hours=getattr(args, "retention_hours", 72))
        serve(config, getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8484))
        return 0
    if cmd == "update-items":
        print("Скачиваю справочник предметов…")
        n = download_catalog(items_path)
        print(f"Готово: {n} предметов -> {items_path}")
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
