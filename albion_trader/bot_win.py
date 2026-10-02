"""Windows для ботов: окна игры, их сетевые порты и ввод мышью/клавиатурой.

* Окна клиента ищутся перебором окон верхнего уровня (EnumWindows) по имени
  процесса ``Albion-Online.exe`` или заголовку «Albion Online».
* Чтобы понять, какой трафик чей, по номеру процесса находится его локальный
  UDP-порт (GetExtendedUdpTable) — пакеты игры с этим портом относятся к этому окну.
* Ввод: окно выводится на передний план, курсор ставится в точку клиентской
  области и нажимается кнопка (SendInput). Можно вернуть прежнее активное окно
  и курсор, чтобы боты меньше мешали за компьютером. Есть экспериментальный
  режим без переключения окон — сообщения окну (PostMessage); не все сборки
  клиента их принимают, калибровка покажет.

Все вызовы Windows идут через объекты ``user32``/``kernel32``/``iphlpapi``,
которые в тестах подменяются.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("albion_trader.bots")

IS_WINDOWS = sys.platform == "win32"
GAME_EXE = "albion-online.exe"
GAME_TITLE = "albion online"

# Коды Windows.
AF_INET = 2
UDP_TABLE_OWNER_PID = 1
ERROR_INSUFFICIENT_BUFFER = 122
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
MK_LBUTTON, MK_RBUTTON = 0x0001, 0x0002
VK_MENU, VK_LBUTTON = 0x12, 0x01

VK = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "space": 0x20,
      "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "home": 0x24, "end": 0x23, "pause": 0x13,
      **{f"f{i}": 0x6F + i for i in range(1, 13)},
      **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
      **{str(d): 0x30 + d for d in range(10)}}


def parse_keys(combo: str) -> list[int]:
    """«ctrl+a» → [VK_CONTROL, 'A']. Неизвестная клавиша — ValueError."""
    out = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part not in VK:
            raise ValueError(f"неизвестная клавиша «{part}»")
        out.append(VK[part])
    return out


def parse_udp_table(buf: bytes) -> list[tuple[int, int]]:
    """Разбор MIB_UDPTABLE_OWNER_PID: [(локальный порт, pid)]."""
    if len(buf) < 4:
        return []
    count = struct.unpack_from("<I", buf, 0)[0]
    out = []
    for i in range(count):
        off = 4 + i * 12
        if off + 12 > len(buf):
            break
        _addr, port_raw, pid = struct.unpack_from("<III", buf, off)
        # Порт записан в сетевом порядке байт в младших 16 битах.
        port = ((port_raw & 0xFF) << 8) | ((port_raw >> 8) & 0xFF)
        out.append((port, pid))
    return out


@dataclass
class GameWindow:
    hwnd: int
    pid: int
    title: str
    exe: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)   # клиентская область на экране: x, y, ширина, высота
    ports: list[int] = field(default_factory=list)

    def point(self, fx: float, fy: float) -> tuple[int, int]:
        """Доли клиентской области → точка экрана."""
        x, y, w, h = self.rect
        fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
        return int(round(x + fx * (w - 1))), int(round(y + fy * (h - 1)))

    def local_point(self, fx: float, fy: float) -> tuple[int, int]:
        sx, sy = self.point(fx, fy)
        return sx - self.rect[0], sy - self.rect[1]

    def to_dict(self) -> dict:
        return {"hwnd": self.hwnd, "pid": self.pid, "title": self.title, "exe": self.exe,
                "rect": list(self.rect), "ports": list(self.ports)}


class WinApi:
    """Ленивая загрузка библиотек Windows (на других системах — недоступно)."""

    def __init__(self, user32=None, kernel32=None, iphlpapi=None):
        self._user32, self._kernel32, self._iphlpapi = user32, kernel32, iphlpapi

    @property
    def available(self) -> bool:
        return IS_WINDOWS or self._user32 is not None

    def _lib(self, name):
        attr = "_" + name
        if getattr(self, attr) is None:
            setattr(self, attr, getattr(getattr(ctypes, "windll"), name))
        return getattr(self, attr)

    @property
    def user32(self):
        return self._lib("user32")

    @property
    def kernel32(self):
        return self._lib("kernel32")

    @property
    def iphlpapi(self):
        return self._lib("iphlpapi")


# --- структуры SendInput ------------------------------------------------------
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def mouse_input(flags: int) -> _INPUT:
    inp = _INPUT(type=INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
    return inp


def key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


class Desktop:
    """Окна игры, их порты и ввод. Методы безопасно возвращают «ничего» вне Windows."""

    def __init__(self, api: WinApi | None = None, sleep=time.sleep):
        self.api = api or WinApi()
        self.sleep = sleep
        self.own_input_at = 0.0      # время последнего своего ввода (time.monotonic)

    @property
    def available(self) -> bool:
        return self.api.available

    # --- окна -----------------------------------------------------------
    def process_exe(self, pid: int) -> str:
        k = self.api.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
            return ""
        finally:
            k.CloseHandle(h)

    def client_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        u = self.api.user32
        r = _RECT()
        u.GetClientRect(hwnd, ctypes.byref(r))
        p = _POINT(0, 0)
        u.ClientToScreen(hwnd, ctypes.byref(p))
        return p.x, p.y, r.right - r.left, r.bottom - r.top

    def game_windows(self) -> list[GameWindow]:
        if not self.available:
            return []
        u = self.api.user32
        found: list[tuple[int, int, str]] = []

        def visit(hwnd, _lparam):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                length = u.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                u.GetWindowTextW(hwnd, buf, length + 1)
                pid = ctypes.c_ulong(0)
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((int(hwnd), int(pid.value), buf.value))
            except Exception:  # pragma: no cover - одно окно не должно ломать перебор
                log.debug("Ошибка при переборе окон", exc_info=True)
            return True

        proto = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        u.EnumWindows(proto(visit), 0)
        ports = self.udp_ports()
        out = []
        for hwnd, pid, title in found:
            exe = self.process_exe(pid)
            if exe.lower() != GAME_EXE and GAME_TITLE not in title.lower():
                continue
            if GAME_TITLE in title.lower() and exe.lower() not in (GAME_EXE, "") and "albion" not in exe.lower():
                continue    # браузер со страницей про Albion Online и т. п.
            out.append(GameWindow(hwnd, pid, title, exe, self.client_rect(hwnd), sorted(ports.get(pid, []))))
        return out

    def udp_ports(self) -> dict[int, set[int]]:
        """pid → локальные UDP-порты процесса."""
        if not self.available:
            return {}
        fn = self.api.iphlpapi.GetExtendedUdpTable
        size = ctypes.c_ulong(0)
        fn(None, ctypes.byref(size), False, AF_INET, UDP_TABLE_OWNER_PID, 0)
        for _ in range(3):
            buf = ctypes.create_string_buffer(max(size.value, 4) + 1024)
            size = ctypes.c_ulong(len(buf))
            rc = fn(buf, ctypes.byref(size), False, AF_INET, UDP_TABLE_OWNER_PID, 0)
            if rc == 0:
                out: dict[int, set[int]] = {}
                for port, pid in parse_udp_table(buf.raw):
                    out.setdefault(pid, set()).add(port)
                return out
            if rc != ERROR_INSUFFICIENT_BUFFER:
                break
        return {}

    def window_alive(self, hwnd: int) -> bool:
        return bool(self.api.user32.IsWindow(hwnd))

    # --- состояние пользователя ------------------------------------------
    def idle_seconds(self) -> float:
        """Сколько секунд не было ввода от человека (свой ввод ботов не считается)."""
        u, k = self.api.user32, self.api.kernel32
        info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
        if not u.GetLastInputInfo(ctypes.byref(info)):
            return 1e9
        since_any = max(0, (k.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
        since_own = time.monotonic() - self.own_input_at
        # Последний ввод — наш (с запасом на задержку) — значит, человек не трогал.
        if since_any + 0.4 >= since_own:
            return 1e9
        return since_any

    def key_down(self, vk: int) -> bool:
        return bool(self.api.user32.GetAsyncKeyState(vk) & 0x8000)

    def foreground(self) -> int:
        return int(self.api.user32.GetForegroundWindow() or 0)

    def cursor(self) -> tuple[int, int]:
        p = _POINT(0, 0)
        self.api.user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    # --- ввод ------------------------------------------------------------
    def focus(self, hwnd: int) -> bool:
        u = self.api.user32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        if self.foreground() == hwnd:
            return True
        # Windows разрешает смену активного окна после нажатия Alt.
        self._send([key_input(VK_MENU), key_input(VK_MENU, flags=KEYEVENTF_KEYUP)])
        u.SetForegroundWindow(hwnd)
        for _ in range(10):
            if self.foreground() == hwnd:
                return True
            self.sleep(0.02)
        return False

    def _send(self, inputs: list) -> None:
        arr = (_INPUT * len(inputs))(*inputs)
        self.api.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
        self.own_input_at = time.monotonic()

    def click(self, win: GameWindow, fx: float, fy: float, button: str = "left", background: bool = False) -> None:
        down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right"
                    else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
        if background:
            x, y = win.local_point(fx, fy)
            wd, wu, mk = ((WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON) if button == "right"
                          else (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON))
            post = self.api.user32.PostMessageW
            post(win.hwnd, WM_MOUSEMOVE, 0, lparam(x, y))
            post(win.hwnd, wd, mk, lparam(x, y))
            self.sleep(0.05)
            post(win.hwnd, wu, 0, lparam(x, y))
            self.own_input_at = time.monotonic()
            return
        x, y = win.point(fx, fy)
        self.api.user32.SetCursorPos(x, y)
        self.own_input_at = time.monotonic()
        self.sleep(0.03)
        self._send([mouse_input(down)])
        self.sleep(0.05)
        self._send([mouse_input(up)])

    def move_cursor(self, x: int, y: int) -> None:
        self.api.user32.SetCursorPos(x, y)
        self.own_input_at = time.monotonic()

    def type_text(self, win: GameWindow, text: str, background: bool = False) -> None:
        for ch in text:
            if background:
                self.api.user32.PostMessageW(win.hwnd, WM_CHAR, ord(ch), 0)
                self.own_input_at = time.monotonic()
            else:
                code = ord(ch)
                self._send([key_input(scan=code, flags=KEYEVENTF_UNICODE),
                            key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
            self.sleep(0.02)

    def press(self, win: GameWindow, combo: str, background: bool = False) -> None:
        keys = parse_keys(combo)
        if background:
            post = self.api.user32.PostMessageW
            for vk in keys:
                post(win.hwnd, WM_KEYDOWN, vk, 0)
            for vk in reversed(keys):
                post(win.hwnd, WM_KEYUP, vk, 0)
            self.own_input_at = time.monotonic()
            return
        self._send([key_input(vk) for vk in keys] + [key_input(vk, flags=KEYEVENTF_KEYUP) for vk in reversed(keys)])
