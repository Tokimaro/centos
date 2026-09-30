"""Значок в трее Windows и автозапуск — только стандартная библиотека (ctypes).

* Значок: двойной щелчок — открыть интерфейс; правая кнопка — меню «Открыть»,
  «Окно-компаньон», «Запускать вместе с Windows», «Выход». Оповещения дублируются всплывающими
  уведомлениями Windows, даже если браузер закрыт.
* Автозапуск — задача Планировщика с наивысшими правами (запуск при входе в
  систему без запроса UAC; обычный ключ реестра Run программы с правами
  администратора не запускает).

На других системах модуль ничего не делает.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path, PureWindowsPath
from typing import Callable

log = logging.getLogger("albion_trader.tray")

TASK_NAME = "AlbionTrader"
IS_WINDOWS = sys.platform == "win32"


# --- автозапуск -------------------------------------------------------------

def launch_command() -> str:
    """Команда запуска приложения: сам .exe, либо pythonw -m albion_trader serve."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    exe = PureWindowsPath(sys.executable)  # функция нужна только в Windows
    pythonw = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" else exe
    workdir = Path(__file__).resolve().parent.parent
    return f'"{pythonw}" -m albion_trader --data-dir "{workdir / "data"}" serve --tray'


def autostart_commands(enable: bool) -> list[str]:
    if enable:
        return ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", launch_command(), "/SC", "ONLOGON",
                "/RL", "HIGHEST", "/F"]
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def autostart_enabled(run=subprocess.run) -> bool:
    if not IS_WINDOWS:
        return False
    res = run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True)
    return res.returncode == 0


def set_autostart(enable: bool, run=subprocess.run) -> bool:
    if not IS_WINDOWS:
        raise OSError("автозапуск настраивается только в Windows")
    res = run(autostart_commands(enable), capture_output=True, text=True)
    if res.returncode != 0 and enable:
        raise OSError((res.stderr or res.stdout or "schtasks завершился с ошибкой").strip())
    return enable


# --- значок в трее ------------------------------------------------------------

class Tray:
    """Значок в области уведомлений. ``start()`` запускает цикл сообщений в отдельном потоке."""

    ID_OPEN, ID_AUTOSTART, ID_EXIT, ID_WINDOW = 1001, 1002, 1003, 1004

    def __init__(self, url: str, on_exit: Callable[[], None], icon_path: str | None = None,
                 on_window: Callable[[], object] | None = None):
        self.url = url
        self.on_exit = on_exit
        self.on_window = on_window
        self.icon_path = icon_path
        self.hwnd = None
        self._ready = threading.Event()
        self._nid = None

    def start(self) -> bool:
        if not IS_WINDOWS:
            return False
        threading.Thread(target=self._run, daemon=True, name="tray").start()
        return self._ready.wait(5)

    def open(self) -> None:
        webbrowser.open(self.url)

    def notify(self, title: str, text: str) -> None:
        """Всплывающее уведомление Windows от значка в трее."""
        if not (IS_WINDOWS and self._nid is not None):
            return
        import ctypes
        nid = self._nid
        nid.uFlags = 0x10  # NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = 1  # NIIF_INFO
        ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY

    # Всё, что ниже, выполняется только в Windows.
    def _run(self) -> None:  # pragma: no cover - требует Windows
        import ctypes
        from ctypes import wintypes as wt

        user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.restype = wt.HWND
        user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        user32.LoadIconW.restype = wt.HICON
        user32.LoadImageW.restype = wt.HANDLE
        user32.CreatePopupMenu.restype = wt.HMENU
        user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                          wt.LPVOID]
        kernel32.GetModuleHandleW.restype = wt.HMODULE

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]

        WM_TRAY = 0x0400 + 20
        hinst = kernel32.GetModuleHandleW(None)

        def menu():
            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, 0, self.ID_OPEN, "Открыть Albion Trader")
            if self.on_window:
                user32.AppendMenuW(hmenu, 0, self.ID_WINDOW, "Окно-компаньон (инструменты)")
            try:
                checked = autostart_enabled()
            except OSError:
                checked = False
            user32.AppendMenuW(hmenu, 0x8 if checked else 0, self.ID_AUTOSTART, "Запускать вместе с Windows")
            user32.AppendMenuW(hmenu, 0x800, 0, None)
            user32.AppendMenuW(hmenu, 0, self.ID_EXIT, "Выход")
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(hmenu, 0x0100 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)
            user32.DestroyMenu(hmenu)
            if cmd == self.ID_OPEN:
                self.open()
            elif cmd == self.ID_WINDOW and self.on_window:
                threading.Thread(target=self.on_window, daemon=True).start()
            elif cmd == self.ID_AUTOSTART:
                try:
                    set_autostart(not checked)
                except OSError as e:
                    self.notify("Автозапуск", str(e))
            elif cmd == self.ID_EXIT:
                user32.DestroyWindow(self.hwnd)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == 0x0203:      # WM_LBUTTONDBLCLK
                    self.open()
                elif lparam == 0x0205:    # WM_RBUTTONUP
                    menu()
                return 0
            if msg == 0x0002:             # WM_DESTROY
                shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid))
                user32.PostQuitMessage(0)
                threading.Thread(target=self.on_exit, daemon=True).start()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)  # держим ссылку, иначе колбэк соберёт сборщик мусора
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=hinst,
                         lpszClassName="AlbionTraderTray")
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "AlbionTraderTray", "Albion Trader", 0, 0, 0, 0, 0, None, None,
                                           hinst, None)
        icon = None
        if self.icon_path and Path(self.icon_path).exists():
            icon = user32.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x10 | 0x40)
        if not icon:
            icon = user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                              uFlags=0x1 | 0x2 | 0x4, uCallbackMessage=WM_TRAY, hIcon=icon)
        nid.szTip = "Albion Trader — двойной щелчок, чтобы открыть"
        shell32.Shell_NotifyIconW(0, ctypes.byref(nid))
        self._nid = nid
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
