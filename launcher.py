"""Точка входа для AlbionTrader.exe (PyInstaller).

Без аргументов: данные в %LOCALAPPDATA%\\AlbionTrader\\data, значок в трее,
браузер открывается сам, справочники скачиваются при первом запуске, лог — в файл.
"""

import os
import sys
from pathlib import Path

from albion_trader.__main__ import main


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AlbionTrader" / "data"


def default_args() -> list[str]:
    return ["--data-dir", str(default_data_dir()), "serve", "--tray", "--open-browser",
            "--fetch-reference", "--log-file"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or default_args()))
