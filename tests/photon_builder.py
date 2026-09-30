"""Сборка пакетов Photon/Protocol18 для тестов (зеркало парсера)."""

import json
import struct


def varuint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(v: int) -> bytes:
    return varuint((v << 1) ^ (v >> 63))


def string(s: str) -> bytes:
    b = s.encode()
    return varuint(len(b)) + b


def value(v) -> bytes:
    """Тип + значение."""
    if v is None:
        return bytes([8])
    if isinstance(v, bool):
        return bytes([28 if v else 27])
    if isinstance(v, int):
        # 32-битные — compressed int (9), большие (тики времени) — compressed long (10).
        return bytes([9 if -2**31 <= v < 2**31 else 10]) + zigzag(v)
    if isinstance(v, float):
        return bytes([5]) + struct.pack("<f", v)
    if isinstance(v, str):
        return bytes([7]) + string(v)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return bytes([0x40 | 7]) + varuint(len(v)) + b"".join(string(x) for x in v)
    if isinstance(v, list) and all(isinstance(x, int) for x in v):
        return bytes([0x40 | 10]) + varuint(len(v)) + b"".join(zigzag(x) for x in v)
    raise TypeError(v)


def params(d: dict) -> bytes:
    return varuint(len(d)) + b"".join(bytes([k]) + value(v) for k, v in d.items())


def command(msg_type: int, body: bytes) -> bytes:
    data = bytes([0, msg_type]) + body
    return bytes([6, 0, 0, 0]) + struct.pack(">II", 12 + len(data), 1) + data


def packet(*commands: bytes, flags: int = 0) -> bytes:
    return struct.pack(">hBBii", 7, flags, len(commands), 0, 99) + b"".join(commands)


def request(op_code: int, p: dict) -> bytes:
    return command(2, bytes([1]) + params({**p, 253: op_code}))


def response(op_code: int, p: dict, return_code: int = 0) -> bytes:
    return command(3, bytes([1]) + struct.pack("<h", return_code) + bytes([8]) + params({**p, 253: op_code}))


def event(code: int, p: dict) -> bytes:
    return command(4, bytes([1]) + params({**p, 252: code}))


def orders_response(orders: list[dict]) -> bytes:
    """Как в Albion: заказы строковым массивом на месте debug-сообщения."""
    strings = [json.dumps(o) for o in orders]
    return command(3, bytes([1]) + struct.pack("<h", 0) + value(strings) + params({}))


def ip_udp(payload: bytes, src_port=5056, dst_port=50000) -> bytes:
    udp = struct.pack(">HHHH", src_port, dst_port, 8 + len(payload), 0) + payload
    total = 20 + len(udp)
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 1, 0, 64, 17, 0,
                     bytes([5, 188, 125, 1]), bytes([192, 168, 1, 2]))
    return ip + udp
