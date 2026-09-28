"""Разбор протокола Photon (Protocol18), которым общается клиент Albion Online.

Порт ``client/photon`` из albiondata-client (MIT License,
Copyright (c) 2017 The Albion Data Project), который, в свою очередь, основан
на PhotonParser.cs из JPCodeCraft/AlbionDataAvalonia.
"""

from __future__ import annotations

import struct
from typing import Callable

PHOTON_HEADER_LENGTH = 12
COMMAND_HEADER_LENGTH = 12
FRAGMENT_HEADER_LENGTH = 20
MAX_PENDING_SEGMENTS = 64
MAX_SEGMENT_TOTAL_LENGTH = 8 << 20

CMD_DISCONNECT = 4
CMD_SEND_RELIABLE = 6
CMD_SEND_UNRELIABLE = 7
CMD_SEND_FRAGMENT = 8

MSG_REQUEST = 2
MSG_RESPONSE = 3
MSG_EVENT = 4
MSG_RESPONSE_ALT = 7
MSG_ENCRYPTED = 131

FLAG_ENCRYPTED = 1
FLAG_CRC_ENABLED = 0xCC
CRC_FIELD_LENGTH = 4

# Коды типов Protocol18
T_UNKNOWN, T_BOOLEAN, T_BYTE, T_SHORT, T_FLOAT, T_DOUBLE, T_STRING, T_NULL = 0, 2, 3, 4, 5, 6, 7, 8
T_COMPRESSED_INT, T_COMPRESSED_LONG = 9, 10
T_INT1, T_INT1_NEG, T_INT2, T_INT2_NEG = 11, 12, 13, 14
T_LONG1, T_LONG1_NEG, T_LONG2, T_LONG2_NEG = 15, 16, 17, 18
T_CUSTOM, T_DICTIONARY, T_HASHTABLE, T_OBJECT_ARRAY = 19, 20, 21, 23
T_OP_REQUEST, T_OP_RESPONSE, T_EVENT_DATA = 24, 25, 26
T_BOOL_FALSE, T_BOOL_TRUE, T_SHORT_ZERO, T_INT_ZERO = 27, 28, 29, 30
T_LONG_ZERO, T_FLOAT_ZERO, T_DOUBLE_ZERO, T_BYTE_ZERO = 31, 32, 33, 34
T_ARRAY = 0x40
T_CUSTOM_SLIM_BASE = 0x80

_ZEROS = {T_BOOL_FALSE: False, T_BOOL_TRUE: True, T_SHORT_ZERO: 0, T_INT_ZERO: 0,
          T_LONG_ZERO: 0, T_FLOAT_ZERO: 0.0, T_DOUBLE_ZERO: 0.0, T_BYTE_ZERO: 0}


class Reader:
    """Минимальный аналог bytes.Buffer: чтение за пределами данных даёт нули."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def byte(self) -> int | None:
        if self.pos >= len(self.data):
            return None
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def unpack(self, fmt: str, size: int):
        chunk = self.take(size)
        if len(chunk) < size:
            return 0
        return struct.unpack(fmt, chunk)[0]

    def varuint(self, max_shift: int = 35) -> int:
        value = shift = 0
        while True:
            b = self.byte()
            if b is None:
                return 0
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7
            if shift >= max_shift:
                return 0

    def varint32(self) -> int:
        v = self.varuint(35)
        return (v >> 1) ^ -(v & 1)

    def varint64(self) -> int:
        v = self.varuint(70)
        return (v >> 1) ^ -(v & 1)

    def string(self) -> str:
        n = self.varuint()
        if n <= 0 or n > self.remaining():
            return ""
        return self.take(n).decode("utf-8", errors="replace")


def read_parameter_table(r: Reader) -> dict:
    count = r.varuint()
    params = {}
    for _ in range(count):
        if r.remaining() <= 0:
            break
        key = r.byte()
        tc = r.byte()
        if key is None or tc is None:
            break
        params[key] = deserialize(r, tc)
    return params


def deserialize(r: Reader, tc: int):
    if tc >= T_CUSTOM_SLIM_BASE:
        return _custom(r, tc & 0x7F, True)
    if tc in (T_UNKNOWN, T_NULL):
        return None
    if tc in _ZEROS:
        return _ZEROS[tc]
    if tc == T_BOOLEAN:
        return (r.byte() or 0) != 0
    if tc == T_BYTE:
        return r.byte() or 0
    if tc == T_SHORT:
        return r.unpack("<h", 2)
    if tc == T_FLOAT:
        return r.unpack("<f", 4)
    if tc == T_DOUBLE:
        return r.unpack("<d", 8)
    if tc == T_STRING:
        return r.string()
    if tc == T_COMPRESSED_INT:
        return r.varint32()
    if tc == T_COMPRESSED_LONG:
        return r.varint64()
    if tc in (T_INT1, T_LONG1):
        return r.byte() or 0
    if tc in (T_INT1_NEG, T_LONG1_NEG):
        return -(r.byte() or 0)
    if tc in (T_INT2, T_LONG2):
        return r.unpack("<H", 2)
    if tc in (T_INT2_NEG, T_LONG2_NEG):
        return -r.unpack("<H", 2)
    if tc == T_CUSTOM:
        return _custom(r, r.byte() or 0, False)
    if tc in (T_DICTIONARY, T_HASHTABLE):
        return _dictionary(r)
    if tc == T_OBJECT_ARRAY:
        out = []
        for _ in range(r.varuint()):
            t = r.byte()
            if t is None:
                break
            out.append(deserialize(r, t))
        return out
    if tc == T_OP_REQUEST:
        code = r.byte()
        return {"operationCode": code, "parameters": read_parameter_table(r)}
    if tc == T_OP_RESPONSE:
        if r.remaining() < 3:
            return None
        code = r.byte()
        rc = r.unpack("<h", 2)
        debug = ""
        if r.remaining() > 0:
            v = deserialize(r, r.byte())
            debug = v if isinstance(v, str) else ""
        return {"operationCode": code, "returnCode": rc, "debugMessage": debug,
                "parameters": read_parameter_table(r)}
    if tc == T_EVENT_DATA:
        code = r.byte()
        return {"code": code, "parameters": read_parameter_table(r)}
    if tc == T_ARRAY:
        size = r.varuint()
        t = r.byte()
        if t is None:
            return None
        size = _clamp(size, r, t in _ZEROS or t in (T_NULL, T_UNKNOWN))
        return [deserialize(r, t) for _ in range(size)]
    if tc & T_ARRAY:
        return _typed_array(r, tc & ~T_ARRAY)
    return f"ERROR - unknown type 0x{tc:02X}"


def _clamp(size: int, r: Reader, zero_width: bool = False) -> int:
    # Защита от мусорных длин: элемент занимает хотя бы байт, если он не нулевой ширины.
    return min(size, 100_000 if zero_width else max(r.remaining(), 0))


def _typed_array(r: Reader, elem: int):
    size = r.varuint()
    if elem == T_BOOLEAN:
        size = min(size, max(r.remaining(), 0) * 8)
    else:
        size = _clamp(size, r, elem in _ZEROS or elem in (T_NULL, T_UNKNOWN))
    if elem == T_BOOLEAN:
        packed = r.take((size + 7) // 8)
        return [bool(packed[i // 8] & (1 << (i % 8))) if i // 8 < len(packed) else False
                for i in range(size)]
    if elem == T_BYTE:
        return r.take(size)
    if elem == T_SHORT:
        return [r.unpack("<h", 2) for _ in range(size)]
    if elem == T_FLOAT:
        return [r.unpack("<f", 4) for _ in range(size)]
    if elem == T_DOUBLE:
        return [r.unpack("<d", 8) for _ in range(size)]
    if elem == T_STRING:
        return [r.string() for _ in range(size)]
    if elem == T_CUSTOM:
        cid = r.byte() or 0
        return [_custom_payload(r, cid, False) for _ in range(size)]
    if elem in (T_DICTIONARY, T_HASHTABLE):
        return [_dictionary(r) for _ in range(size)]
    if elem == T_COMPRESSED_INT:
        return [r.varint32() for _ in range(size)]
    if elem == T_COMPRESSED_LONG:
        return [r.varint64() for _ in range(size)]
    return [deserialize(r, elem) for _ in range(size)]


def _dictionary(r: Reader) -> dict:
    key_tc = r.byte() or 0
    val_tc = r.byte() or 0
    count = r.varuint()
    out = {}
    for i in range(count):
        if r.remaining() <= 0:
            break
        kt = r.byte() if key_tc == 0 else key_tc
        vt = r.byte() if val_tc == 0 else val_tc
        key = deserialize(r, kt or 0)
        val = deserialize(r, vt or 0)
        try:
            out[key] = val
        except TypeError:
            out[f"UNHASHABLE_{i}"] = val
    return out


def _custom(r: Reader, cid: int, slim: bool):
    return _custom_payload(r, cid, slim)


def _custom_payload(r: Reader, cid: int, slim: bool):
    size = r.varuint()
    if size < 0 or size > r.remaining():
        if slim:
            return {"type": cid, "data": r.take(r.remaining())}
        return None
    return {"type": cid, "data": r.take(size)}


def photon_crc(data: bytes) -> int:
    result = 0xFFFFFFFF
    for b in data:
        result ^= b
        for _ in range(8):
            result = (result >> 1) ^ 0xEDB88320 if result & 1 else result >> 1
    return result


class PhotonParser:
    """Принимает UDP-полезную нагрузку и вызывает колбэки для запросов,
    ответов и событий Photon."""

    def __init__(self,
                 on_request: Callable[[int, dict], None] | None = None,
                 on_response: Callable[[int, int, str, dict], None] | None = None,
                 on_event: Callable[[int, dict], None] | None = None,
                 on_encrypted: Callable[[], None] | None = None):
        self.on_request = on_request
        self.on_response = on_response
        self.on_event = on_event
        self.on_encrypted = on_encrypted
        self.pending: dict[int, dict] = {}
        self.evicted_segments = 0  # недособранные сообщения (потерян кусок)

    # --- пакеты -------------------------------------------------------
    def receive_packet(self, payload: bytes) -> bool:
        if len(payload) < PHOTON_HEADER_LENGTH:
            return False
        ident = _identity(payload, 0)
        first = _packet_length(payload, 0)
        if ident is None or first is None:
            return False
        first_len, terminal = first
        if terminal or first_len == len(payload):
            return self._receive_single(payload)

        # Несколько пакетов Photon в одной UDP-датаграмме.
        ranges = [(0, first_len)]
        off = first_len
        framing_ok = True
        while off < len(payload):
            if _identity(payload, off) != ident:
                framing_ok = False
                break
            nxt = _packet_length(payload, off)
            if nxt is None:
                framing_ok = False
                break
            ranges.append((off, nxt[0]))
            off += nxt[0]
            if nxt[1]:
                break
        ok = framing_ok
        for start, length in ranges:
            if not self._receive_single(payload[start:start + length]):
                ok = False
        return ok

    def _receive_single(self, payload: bytes) -> bool:
        if len(payload) < PHOTON_HEADER_LENGTH:
            return False
        flags = payload[2]
        command_count = payload[3]
        offset = PHOTON_HEADER_LENGTH
        if flags == FLAG_ENCRYPTED:
            if self.on_encrypted:
                self.on_encrypted()
            return False
        if flags == FLAG_CRC_ENABLED:
            if len(payload) < offset + CRC_FIELD_LENGTH:
                return False
            crc = struct.unpack_from(">I", payload, offset)[0]
            offset += CRC_FIELD_LENGTH
            zeroed = bytearray(payload)
            zeroed[PHOTON_HEADER_LENGTH:PHOTON_HEADER_LENGTH + CRC_FIELD_LENGTH] = b"\0\0\0\0"
            if crc != photon_crc(zeroed):
                return False
        for _ in range(command_count):
            offset = self._handle_command(payload, offset)
            if offset is None:
                return False
        return offset == len(payload)

    def _handle_command(self, src: bytes, offset: int) -> int | None:
        if len(src) - offset < COMMAND_HEADER_LENGTH:
            return None
        cmd_type = src[offset]
        cmd_len = struct.unpack_from(">I", src, offset + 4)[0] - COMMAND_HEADER_LENGTH
        offset += COMMAND_HEADER_LENGTH
        if cmd_len < 0 or len(src) - offset < cmd_len:
            return None
        if cmd_type == CMD_SEND_UNRELIABLE:
            if cmd_len < 4:
                return None
            self._handle_reliable(src[offset + 4:offset + cmd_len])
        elif cmd_type == CMD_SEND_RELIABLE:
            self._handle_reliable(src[offset:offset + cmd_len])
        elif cmd_type == CMD_SEND_FRAGMENT:
            self._handle_fragment(src[offset:offset + cmd_len])
        return offset + cmd_len

    def _handle_reliable(self, data: bytes) -> None:
        if len(data) < 2:
            return
        msg_type = data[1]
        body = data[2:]
        if msg_type == MSG_ENCRYPTED:
            if self.on_encrypted:
                self.on_encrypted()
            return
        if not body:
            return
        if msg_type == MSG_REQUEST:
            if self.on_request:
                self.on_request(body[0], read_parameter_table(Reader(body, 1)))
        elif msg_type in (MSG_RESPONSE, MSG_RESPONSE_ALT):
            self._dispatch_response(body)
        elif msg_type == MSG_EVENT:
            if self.on_event:
                self.on_event(body[0], read_parameter_table(Reader(body, 1)))

    def _dispatch_response(self, body: bytes) -> None:
        if len(body) < 3 or not self.on_response:
            return
        op_code = body[0]
        return_code = struct.unpack_from("<h", body, 1)[0]
        r = Reader(body, 3)
        debug = ""
        if r.remaining() > 0:
            val = deserialize(r, r.byte())
            if isinstance(val, str):
                debug = val
            elif isinstance(val, list) and val and all(isinstance(v, str) for v in val):
                # Albion кладёт заказы рынка строковым массивом на место debug-сообщения.
                self.on_response(op_code, return_code, "", {0: val})
                return
        self.on_response(op_code, return_code, debug, read_parameter_table(r))

    def _handle_fragment(self, data: bytes) -> None:
        if len(data) < FRAGMENT_HEADER_LENGTH:
            return
        start_seq, count, num, total_len, frag_offset = struct.unpack_from(">IIIII", data, 0)
        frag = data[FRAGMENT_HEADER_LENGTH:]
        seg = self.pending.get(start_seq)
        if seg is None:
            if total_len <= 0 or total_len > MAX_SEGMENT_TOTAL_LENGTH:
                return
            if len(self.pending) >= MAX_PENDING_SEGMENTS:
                # dict сохраняет порядок вставки — удаляем самую старую сборку.
                self.pending.pop(next(iter(self.pending)))
                self.evicted_segments += 1
            seg = {"total": total_len, "written": 0, "buf": bytearray(total_len), "seen": set()}
            self.pending[start_seq] = seg
        # Повторы одного и того же куска (переотправка, дубли на разных
        # сетевых адаптерах) не должны засчитываться дважды — иначе сообщение
        # «соберётся» раньше времени с дырами.
        key = (num, frag_offset)
        if key in seg["seen"]:
            return
        seg["seen"].add(key)
        end = frag_offset + len(frag)
        if end <= len(seg["buf"]):
            seg["buf"][frag_offset:end] = frag
        seg["written"] += len(frag)
        if seg["written"] >= seg["total"] or (count and len(seg["seen"]) >= count
                                              and seg["written"] >= seg["total"]):
            del self.pending[start_seq]
            self._handle_reliable(bytes(seg["buf"]))

def _identity(payload: bytes, offset: int):
    if len(payload) - offset < PHOTON_HEADER_LENGTH:
        return None
    return struct.unpack_from(">h", payload, offset)[0], struct.unpack_from(">i", payload, offset + 8)[0]


def _packet_length(payload: bytes, offset: int):
    """(длина пакета, зашифрован_и_последний) или None."""
    if len(payload) - offset < PHOTON_HEADER_LENGTH:
        return None
    flags = payload[offset + 2]
    if flags == FLAG_ENCRYPTED:
        return len(payload) - offset, True
    header_len = PHOTON_HEADER_LENGTH + (CRC_FIELD_LENGTH if flags == FLAG_CRC_ENABLED else 0)
    if len(payload) - offset < header_len:
        return None
    count = payload[offset + 3]
    if count == 0:
        return None
    cmd = offset + header_len
    for _ in range(count):
        if len(payload) - cmd < COMMAND_HEADER_LENGTH:
            return None
        cmd_len = struct.unpack_from(">I", payload, cmd + 4)[0]
        if cmd_len < COMMAND_HEADER_LENGTH or len(payload) - cmd < cmd_len:
            return None
        cmd += cmd_len
    return cmd - offset, False
