"""Точка входа для AlbionTrader.exe (PyInstaller).

* Без аргументов: данные в %LOCALAPPDATA%\\AlbionTrader\\data (или ALBION_TRADER_DATA),
  значок в трее, браузер открывается сам, справочники скачиваются при первом запуске,
  лог — в файл.
* ``--autostart`` — то же, но без открытия браузера (так запускает Планировщик при входе).
* С другими аргументами — они передаются как есть; недостающий ``--data-dir``
  подставляется, чтобы .exe не терял базу.
* Если приложение уже запущено, второй экземпляр просто открывает его страницу.
"""

import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8484


def default_data_dir() -> Path:
    env = os.environ.get("ALBION_TRADER_DATA")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AlbionTrader" / "data"


def default_args(autostart: bool = False) -> list:
    args = ["--data-dir", str(default_data_dir()), "serve", "--tray", "--fetch-reference", "--log-file"]
    if not autostart:
        args.append("--open-browser")
    return args


def build_args(argv: list) -> list:
    if not argv:
        return default_args()
    if argv == ["--autostart"]:
        return default_args(autostart=True)
    if "--data-dir" not in argv:
        return ["--data-dir", str(default_data_dir())] + argv
    return argv


def serve_port(args: list) -> int | None:
    """Порт, если запускается сервер (``serve`` или без команды), иначе None."""
    commands = {"serve", "replay", "update-items", "update-opcodes", "cleanup", "check"}
    cmd = next((a for a in args if a in commands), "serve")
    if cmd != "serve":
        return None
    for i, a in enumerate(args):
        if a == "--port" or a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1] if "=" in a else args[i + 1])
            except (IndexError, ValueError):
                return DEFAULT_PORT
    return DEFAULT_PORT


def already_running(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system", timeout=timeout) as r:
            return "version" in json.load(r)
    except urllib.error.HTTPError as e:
        return e.code == 401        # запущен, но интерфейс под паролем
    except (OSError, ValueError):
        return False


def _redirect_output(data_dir: Path) -> None:
    """У .exe без консоли нет stderr: ошибки запуска и argparse пишем в launcher.log."""
    if sys.stderr is None or sys.stdout is None:
        data_dir.mkdir(parents=True, exist_ok=True)
        stream = open(data_dir / "launcher.log", "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stderr or stream
        sys.stdout = sys.stdout or stream


def run(argv: list) -> int:
    args = build_args(argv)
    data_dir = Path(args[args.index("--data-dir") + 1]) if "--data-dir" in args else default_data_dir()
    _redirect_output(data_dir)
    port = serve_port(args)
    if port and already_running(port):
        if argv != ["--autostart"]:
            webbrowser.open(f"http://127.0.0.1:{port}")
        return 0
    from albion_trader.__main__ import main
    try:
        return main(args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
