"""Перемотка записи трафика на радаре: воспроизведение .pcap с паузой, скоростью и
переходом к любому моменту.

Запись проигрывается через свой экземпляр сборщика и радара (рыночные данные из
записи не сохраняются). Назад перематывается пересборкой с начала — для записей в
несколько десятков мегабайт это доли секунды.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Callable

from .capture.albion import AlbionState
from .capture.photon import PhotonParser
from .capture.sniffer import ALBION_PORTS, CaptureError, _strip_link_layer, parse_ipv4_udp


def read_pcap_timed(path: str | Path, ports=ALBION_PORTS) -> list[tuple[float, bytes]]:
    """[(время, полезная нагрузка UDP)] из классического .pcap — только трафик Albion."""
    out = []
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
            raise CaptureError("поддерживается только формат .pcap")
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        linktype = struct.unpack(endian + "I", header[20:24])[0]
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                break
            sec, frac, incl, _ = struct.unpack(endian + "IIII", rec)
            frame = f.read(incl)
            ip = _strip_link_layer(frame, linktype)
            payload = parse_ipv4_udp(ip, ports) if ip else None
            if payload:
                out.append((sec + frac / (1e9 if nano else 1e6), payload))
    return out


class RadarReplay:
    def __init__(self, path: str | Path, make_radar: Callable[[], object], opcodes: dict,
                 clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self.packets = read_pcap_timed(path)
        if not self.packets:
            raise CaptureError("в записи нет трафика Albion (UDP 5056)")
        self.make_radar = make_radar
        self.opcodes = opcodes
        self.clock = clock
        self.t0 = self.packets[0][0]
        self.duration = self.packets[-1][0] - self.t0
        self.speed = 1.0
        self.playing = False
        self._reset()

    def _reset(self) -> None:
        self.state = AlbionState(lambda *_: 0, dict(self.opcodes))
        self.state.moves_until = float("inf")       # в записи разбираем всё
        self.radar = self.make_radar()
        self.radar.clock = lambda: self.t0 + self.pos   # «время» радара — время записи
        self.radar.attach(self.state)
        self.parser = PhotonParser(self.state.on_request, self.state.on_response, self.state.on_event,
                                   event_filter=self.state.accepts_event)
        self.index = 0
        self.pos = 0.0
        self._wall = self.clock()

    def _feed_until(self, pos: float) -> None:
        self.pos = max(0.0, min(pos, self.duration))
        limit = self.t0 + self.pos
        while self.index < len(self.packets) and self.packets[self.index][0] <= limit:
            try:
                self.parser.receive_packet(self.packets[self.index][1])
            except Exception:   # pragma: no cover - битый пакет не должен ронять проигрывание
                pass
            self.index += 1

    def advance(self) -> None:
        now = self.clock()
        if self.playing:
            self._feed_until(self.pos + (now - self._wall) * self.speed)
            if self.pos >= self.duration:
                self.playing = False
        self._wall = now

    def seek(self, pos: float) -> None:
        self.advance()
        if pos < self.pos:
            playing, speed = self.playing, self.speed
            self._reset()
            self.playing, self.speed = playing, speed
        self._feed_until(pos)

    def play(self) -> None:
        self.advance()
        if self.pos >= self.duration:
            self.seek(0)
        self.playing = True

    def pause(self) -> None:
        self.advance()
        self.playing = False

    def info(self) -> dict:
        return {"active": True, "file": self.path.name, "pos": round(self.pos, 1), "duration": round(self.duration, 1),
                "playing": self.playing, "speed": self.speed, "packets": len(self.packets)}
