"""Окно-компаньон: инструменты в отдельном окне без вкладок и адресной строки.

Окно открывается браузером на базе Chromium (Edge есть в каждой Windows 10/11)
в режиме приложения (``--app``) со своим профилем — поэтому это отдельное окно,
которое помнит размер и положение и не смешивается с вкладками обычного
браузера. «Поверх всех окон» включается средствами Windows (SetWindowPos) по
заголовку окна. Если подходящего браузера нет, страница открывается в обычном.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable

log = logging.getLogger("albion_trader.window")

IS_WINDOWS = sys.platform == "win32"
TITLE = "Albion Trader — инструменты"   # совпадает с <title> companion.html
PAGE = "companion.html"
DEFAULT_SIZE = (560, 860)


def browser_candidates(env=None) -> list[str]:
    env = os.environ if env is None else env
    out = []
    if IS_WINDOWS or env.get("ProgramFiles"):
        roots = [env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")]
        for root in filter(None, roots):
            out.append(str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
            out.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
            out.append(str(Path(root) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"))
    out += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return out


LINUX_NAMES = ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
               "brave-browser")


def find_browser(env=None, exists: Callable[[str], bool] = os.path.exists,
                 which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Путь к Edge/Chrome/Chromium/Brave или None."""
    for path in browser_candidates(env):
        if exists(path):
            return path
    for name in LINUX_NAMES:
        found = which(name)
        if found:
            return found
    return None


def app_command(browser: str, url: str, profile_dir: str | Path, size=DEFAULT_SIZE) -> list[str]:
    return [browser, f"--app={url}", f"--user-data-dir={profile_dir}", f"--window-size={size[0]},{size[1]}",
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]


class CompanionWindow:
    def __init__(self, base_url: str, profile_dir: str | Path, popen=subprocess.Popen,
                 finder: Callable[[], str | None] = find_browser, opener=webbrowser.open):
        self.url = base_url.rstrip("/") + "/" + PAGE
        self.profile_dir = Path(profile_dir)
        self.popen = popen
        self.finder = finder
        self.opener = opener
        self.proc = None
        self.topmost = False
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open(self) -> dict:
        """Открыть окно (или вывести уже открытое на передний план)."""
        with self.lock:
            if self.running():
                _activate(TITLE)
                return {"mode": "app", "already": True}
            browser = self.finder()
            if browser:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                try:
                    self.proc = self.popen(app_command(browser, self.url, self.profile_dir),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    log.info("Окно-компаньон открыто (%s)", Path(browser).name)
                    if self.topmost:
                        threading.Thread(target=self._apply_topmost_later, daemon=True).start()
                    return {"mode": "app", "browser": Path(browser).name}
                except OSError as e:
                    log.warning("Не удалось запустить %s: %s", browser, e)
            self.opener(self.url, new=1)
            return {"mode": "browser"}

    def set_topmost(self, on: bool) -> dict:
        self.topmost = bool(on)
        if not IS_WINDOWS:
            return {"topmost": self.topmost, "applied": False, "reason": "только в Windows"}
        applied = _set_topmost(TITLE, self.topmost)
        return {"topmost": self.topmost, "applied": applied,
                "reason": "" if applied else "окно не найдено — откройте его из трея или кнопкой"}

    def _apply_topmost_later(self) -> None:  # pragma: no cover - требует Windows
        for _ in range(40):  # окно появляется не сразу
            time.sleep(0.25)
            if _set_topmost(TITLE, True):
                return


# --- Windows ------------------------------------------------------------------

def _find_windows(title_prefix: str) -> list:  # pragma: no cover - требует Windows
    import ctypes
    from ctypes import wintypes as wt
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(title_prefix):
                found.append(hwnd)
        return True
    user32.EnumWindows(callback, 0)
    return found


def _set_topmost(title_prefix: str, on: bool) -> bool:
    if not IS_WINDOWS:
        return False
    import ctypes  # pragma: no cover - требует Windows
    user32 = ctypes.windll.user32
    hwnds = _find_windows(title_prefix)
    insert_after = ctypes.c_void_p(-1 if on else -2)   # HWND_TOPMOST / HWND_NOTOPMOST
    for hwnd in hwnds:
        user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE
    return bool(hwnds)


def _activate(title_prefix: str) -> bool:
    if not IS_WINDOWS:
        return False
    import ctypes  # pragma: no cover - требует Windows
    user32 = ctypes.windll.user32
    hwnds = _find_windows(title_prefix)
    for hwnd in hwnds:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return bool(hwnds)
