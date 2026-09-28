"""Захват UDP-трафика Albion Online без сторонних программ.

* Windows — «сырой» сокет с ``SIO_RCVALL`` (встроено в систему, Npcap не нужен).
  Требуются права администратора; брандмауэр Windows может спросить разрешение
  для Python — его нужно дать.
* Linux — сокет ``AF_PACKET`` (нужен root или ``CAP_NET_RAW``).
* Файл .pcap (например, из Wireshark) — для отладки: ``python -m albion_trader replay``.

Игра общается с игровыми серверами по UDP-порту 5056.
"""

from __future__ import annotations

import logging
import socket
import struct
import sys
import threading
import time
from typing import Callable, Iterator

from .photon import PhotonParser

log = logging.getLogger("albion_trader.capture")

ALBION_PORTS = (5056,)
ETH_P_IP = 0x0800


class CaptureError(RuntimeError):
    pass


def parse_ipv4_udp(packet: bytes, ports=ALBION_PORTS) -> bytes | None:
    """Возвращает полезную нагрузку UDP, если пакет — IPv4/UDP на нужный порт."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if packet[9] != 17 or len(packet) < ihl + 8:
        return None
    frag = struct.unpack_from(">H", packet, 6)[0]
    if frag & 0x3FFF:  # фрагменты IP не собираем — для Photon они не встречаются
        return None
    total_len = struct.unpack_from(">H", packet, 2)[0]
    src_port, dst_port, udp_len = struct.unpack_from(">HHH", packet, ihl)
    if src_port not in ports and dst_port not in ports:
        return None
    end = min(len(packet), total_len or len(packet), ihl + udp_len if udp_len >= 8 else len(packet))
    return packet[ihl + 8:end]


def local_ipv4_addresses() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        # Адрес интерфейса маршрута по умолчанию (пакеты при этом не отправляются).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _open_windows_sockets() -> list[socket.socket]:
    socks, errors = [], []
    for ip in local_ipv4_addresses():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
            s.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)  # type: ignore[attr-defined]
            s.settimeout(1.0)
            socks.append(s)
            log.info("Захват пакетов на интерфейсе %s", ip)
        except OSError as e:
            errors.append(f"{ip}: {e}")
    if not socks:
        hint = "запустите приложение от имени администратора"
        raise CaptureError(f"не удалось открыть захват ({hint}). " + "; ".join(errors))
    return socks


def _open_linux_socket() -> list[socket.socket]:
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_IP))  # type: ignore[attr-defined]
    except PermissionError as e:
        raise CaptureError("нет прав на захват пакетов: запустите через sudo "
                           "или выдайте python cap_net_raw") from e
    s.settimeout(1.0)
    log.info("Захват пакетов на всех интерфейсах (AF_PACKET)")
    return [s]


def open_capture_sockets() -> list[socket.socket]:
    if sys.platform == "win32":
        return _open_windows_sockets()
    if sys.platform.startswith("linux"):
        return _open_linux_socket()
    raise CaptureError(f"встроенный захват не поддерживается на {sys.platform} "
                       "(используйте внешний albiondata-client)")


class Sniffer:
    """Читает пакеты в фоновых потоках и передаёт их парсеру Photon."""

    def __init__(self, state, ports=ALBION_PORTS,
                 open_sockets: Callable[[], list] = open_capture_sockets):
        self.state = state
        self.ports = ports
        self.open_sockets = open_sockets
        self.parser = PhotonParser(state.on_request, state.on_response, state.on_event,
                                   state.on_encrypted)
        self.parser_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.status = {"running": False, "error": None, "packets": 0,
                       "last_packet_at": None, "started_at": None}

    def start(self) -> bool:
        try:
            socks = self.open_sockets()
        except CaptureError as e:
            self.status["error"] = str(e)
            log.error("Захват не запущен: %s", e)
            return False
        except OSError as e:
            self.status["error"] = f"ошибка открытия захвата: {e}"
            log.error("Захват не запущен: %s", e)
            return False
        self.status.update(running=True, error=None, started_at=time.time())
        for s in socks:
            t = threading.Thread(target=self._loop, args=(s,), daemon=True, name="capture")
            t.start()
            self.threads.append(t)
        return True

    def stop(self) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=3)
        self.status["running"] = False

    def feed_ip_packet(self, packet: bytes) -> None:
        payload = parse_ipv4_udp(packet, self.ports)
        if not payload:
            return
        self.status["packets"] += 1
        self.status["last_packet_at"] = time.time()
        with self.parser_lock:
            try:
                self.parser.receive_packet(payload)
            except Exception:  # pragma: no cover - битый пакет не должен ронять захват
                log.debug("Ошибка разбора пакета", exc_info=True)

    def _loop(self, sock) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as e:
                    self.status["error"] = f"захват остановлен: {e}"
                    log.error("Захват остановлен: %s", e)
                    return
                # На loopback Linux каждый пакет виден дважды (исходящий и входящий).
                if isinstance(addr, tuple) and len(addr) >= 3 and addr[0] == "lo" and addr[2] == 4:
                    continue
                self.feed_ip_packet(data)
        finally:
            if sys.platform == "win32":
                try:
                    sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)  # type: ignore[attr-defined]
                except OSError:
                    pass
            sock.close()


# --- pcap ---------------------------------------------------------------

_LINKTYPE_ETHERNET, _LINKTYPE_RAW, _LINKTYPE_LINUX_SLL, _LINKTYPE_NULL = 1, 101, 113, 0


def read_pcap(path: str) -> Iterator[bytes]:
    """Итератор IP-пакетов из классического .pcap (не .pcapng)."""
    with open(path, "rb") as f:
        header = f.read(24)
        if len(header) < 24:
            raise CaptureError("файл слишком короткий")
        magic = header[:4]
        if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
            endian = "<"
        elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
            endian = ">"
        else:
            raise CaptureError("поддерживается только формат .pcap (в Wireshark: «Сохранить как» → pcap)")
        linktype = struct.unpack(endian + "I", header[20:24])[0]
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            incl_len = struct.unpack(endian + "I", rec[8:12])[0]
            frame = f.read(incl_len)
            ip = _strip_link_layer(frame, linktype)
            if ip:
                yield ip


def _strip_link_layer(frame: bytes, linktype: int) -> bytes | None:
    if linktype == _LINKTYPE_ETHERNET:
        off, ethertype = 12, None
        while len(frame) >= off + 2:
            ethertype = struct.unpack_from(">H", frame, off)[0]
            if ethertype in (0x8100, 0x88A8):  # VLAN
                off += 4
                continue
            break
        return frame[off + 2:] if ethertype == ETH_P_IP else None
    if linktype == _LINKTYPE_RAW:
        return frame
    if linktype == _LINKTYPE_LINUX_SLL:
        return frame[16:] if len(frame) > 16 and struct.unpack_from(">H", frame, 14)[0] == ETH_P_IP else None
    if linktype == _LINKTYPE_NULL:
        return frame[4:]
    return None
