"""Разбор операций Albion Online из потока Photon.

Логика повторяет albiondata-client: текущая локация берётся из ответа на вход
в зону (Join) и запроса GetGameServerByCluster, заказы рынка — из ответов
AuctionGetOffers / AuctionGetRequests, история продаж — из
AuctionGetItemAverageStats. Результат передаётся в ``sink(topic, payload)``
в том же формате, что и у внешнего клиента.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("albion_trader.capture")

# Номера операций из client/operations.go (albiondata-client). После
# обновлений игры они иногда меняются — тогда их можно переопределить файлом
# data/opcodes.json, например: {"auction_get_offers": 81}.
DEFAULT_OPCODES = {
    "join": 2,
    "get_game_server_by_cluster": 17,
    "auction_get_offers": 81,
    "auction_get_requests": 82,
    "auction_buy_offer": 83,
    "auction_get_item_average_stats": 95,
}

HISTORY_CACHE_SIZE = 1024
ENCRYPTION_WINDOW = 3.0
MARKET_RESPONSE_TIMEOUT = 15.0

_RE_ISLAND = re.compile(r"(?i)@island@[0-9a-f-]{36}")
_RE_NUMERIC = re.compile(r"^[0-9]{3,6}$")


def load_opcodes(path: str | Path | None) -> dict:
    codes = dict(DEFAULT_OPCODES)
    if path and Path(path).exists():
        try:
            override = json.loads(Path(path).read_text(encoding="utf-8"))
            codes.update({k: int(v) for k, v in override.items() if k in codes})
            log.info("Коды операций переопределены из %s", path)
        except (ValueError, TypeError, OSError) as e:
            log.warning("Не удалось прочитать %s: %s", path, e)
    return codes


def normalize_location_id(value) -> str:
    """Порт normalizeLocationID: пустая строка, если значение не похоже на локацию."""
    if not isinstance(value, str):
        return ""
    s = value.strip().strip(",.")
    if not s:
        return ""
    m = _RE_ISLAND.search(s)
    if m:
        return "@ISLAND@" + m.group(0)[len("@island@"):]
    if _RE_NUMERIC.match(s):
        return s
    ls = s.lower()
    if (ls.startswith(("island-player-", "@player-island", "@island-"))
            or s.startswith("BLACKBANK-") or s.endswith(("-HellDen", "-Auction2"))):
        return s
    return ""


def is_market_location(loc: str) -> bool:
    return bool(loc) and (loc.isdigit() or loc.startswith("BLACKBANK-")
                          or loc.endswith(("-HellDen", "-Auction2")))


def _as_int(v) -> int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    return None


class AlbionState:
    """Обрабатывает запросы/ответы Photon и отправляет рыночные данные в sink."""

    def __init__(self, sink: Callable[[str, dict], object], opcodes: dict | None = None,
                 clock: Callable[[], float] = time.time):
        self.sink = sink
        self.op = dict(opcodes or DEFAULT_OPCODES)
        self._interesting = set(self.op.values())
        self.clock = clock
        self.lock = threading.Lock()
        self.location = ""
        self.history_lookup: dict[int, tuple[int, int, int]] = {}
        self.last_market_request = 0.0
        # Время отправки запросов рынка, на которые ещё не пришёл ответ.
        # Игра шлёт по 4 запроса на один просмотр, поэтому считаем штучно.
        self.pending_market_requests: list[float] = []
        self._last_location_warning = 0.0
        self.stats = {
            "location": "", "orders": 0, "order_batches": 0, "history_batches": 0,
            "last_data_at": None, "encrypted_at": None, "no_location_drops": 0,
            "market_requests": 0, "market_responses_lost": 0,
        }

    # --- коды -----------------------------------------------------------
    def _normalize(self, code: int) -> int:
        code &= 0xFFFF
        if code in self._interesting:
            return code
        swapped = ((code << 8) | (code >> 8)) & 0xFFFF
        if swapped in self._interesting:
            return swapped
        if code > 0xFF and code & 0xFF == 0:
            return code >> 8
        return code

    def _code(self, params: dict, fallback: int) -> int:
        v = _as_int(params.get(253))
        return self._normalize(fallback if v is None else v)

    # --- колбэки парсера -----------------------------------------------
    def on_request(self, op_code: int, params: dict) -> None:
        code = self._code(params, op_code)
        with self.lock:
            if code == self.op["get_game_server_by_cluster"]:
                self._set_location(params.get(0), "GetGameServerByCluster")
            elif code in (self.op["auction_get_offers"], self.op["auction_get_requests"]):
                now = self.clock()
                self._expire_market_requests(now)
                self.last_market_request = now
                self.pending_market_requests.append(now)
                self.stats["market_requests"] += 1
            elif code == self.op["auction_get_item_average_stats"]:
                self._remember_history_request(params)

    def on_response(self, op_code: int, return_code: int, _debug: str, params: dict) -> None:
        code = self._code(params, op_code)
        orders = params.get(0)
        is_orders = isinstance(orders, list) and all(isinstance(o, str) for o in orders)
        with self.lock:
            if code in (self.op["auction_get_offers"], self.op["auction_get_requests"]) and not is_orders:
                self._market_response_arrived()
            if is_orders and (code in (self.op["auction_get_offers"], self.op["auction_get_requests"],
                                       self.op["auction_buy_offer"]) or set(params) == {0}):
                self._market_orders(orders)
            elif code == self.op["join"]:
                self._set_location(params.get(8), "Join")
            elif code == self.op["get_game_server_by_cluster"]:
                self._set_location(params.get(0), "GetGameServerByCluster")
            elif code == self.op["auction_get_item_average_stats"]:
                self._history_response(params)
            elif code not in self._interesting and normalize_location_id(params.get(8)):
                # После обновлений игры код Join может сместиться — узнаём его по форме.
                self._set_location(params.get(8), "Join?")

    def on_event(self, _code: int, _params: dict) -> None:
        pass

    def _market_response_arrived(self) -> None:
        if self.pending_market_requests:
            self.pending_market_requests.pop(0)

    def _expire_market_requests(self, now: float) -> None:
        while self.pending_market_requests and now - self.pending_market_requests[0] > MARKET_RESPONSE_TIMEOUT:
            self.pending_market_requests.pop(0)
            self._lost_response()

    def _lost_response(self) -> None:
        self.stats["market_responses_lost"] += 1
        lost = self.stats["market_responses_lost"]
        if lost in (1, 5) or lost % 20 == 0:
            log.warning("Ответ рынка не дошёл целиком (потеряно: %d из %d). Похоже, теряются "
                        "пакеты — закройте лишние программы, нагружающие сеть, и попробуйте "
                        "обновить страницу рынка ещё раз.", lost, self.stats["market_requests"])

    def on_encrypted(self) -> None:
        now = self.clock()
        with self.lock:
            if self.last_market_request and now - self.last_market_request <= ENCRYPTION_WINDOW:
                self._market_response_arrived()
                if self.stats["encrypted_at"] is None or now - self.stats["encrypted_at"] > 60:
                    log.warning("Данные рынка пришли зашифрованными — игра не отдаёт их в открытом виде.")
                self.stats["encrypted_at"] = now

    # --- обработчики ----------------------------------------------------
    def _set_location(self, raw, source: str) -> None:
        loc = normalize_location_id(raw)
        if loc and loc != self.location:
            log.info("Текущая локация: %s (%s)", loc, source)
            self.location = loc
            self.stats["location"] = loc

    def _market_orders(self, raw_orders: list[str]) -> None:
        self._market_response_arrived()
        orders = []
        for s in raw_orders:
            try:
                o = json.loads(s)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            own = o.get("LocationId")
            if not (isinstance(own, str) and own) and not isinstance(own, int):
                if not is_market_location(self.location):
                    continue
                o["LocationId"] = self.location
            orders.append(o)
        if not orders:
            if raw_orders:
                self.stats["no_location_drops"] += 1
                now = self.clock()
                if now - self._last_location_warning < 30:
                    return
                self._last_location_warning = now
                log.warning("Не знаю текущую локацию — смените зону (войдите/выйдите из города), "
                            "чтобы привязать цены к рынку.")
            return
        self.stats["orders"] += len(orders)
        self.stats["order_batches"] += 1
        self.stats["last_data_at"] = self.clock()
        self.stats["encrypted_at"] = None
        self._emit("marketorders.ingest", {"Orders": orders})

    def _remember_history_request(self, params: dict) -> None:
        msg_id = _as_int(params.get(255))
        item_id = _as_int(params.get(1))
        if msg_id is None or item_id is None:
            return
        if -129 < item_id < 0:
            item_id += 256
        self.history_lookup[msg_id % HISTORY_CACHE_SIZE] = (
            item_id, _as_int(params.get(2)) or 0, _as_int(params.get(3)) or 0)

    def _history_response(self, params: dict) -> None:
        msg_id = _as_int(params.get(255))
        if msg_id is None:
            return
        info = self.history_lookup.pop(msg_id % HISTORY_CACHE_SIZE, None)
        if not info or not is_market_location(self.location):
            return
        amounts, silver, stamps = params.get(0) or [], params.get(1) or [], params.get(2) or []
        histories = []
        for amount, sv, ts in zip(amounts, silver, stamps):
            amount = _as_int(amount)
            if amount is None:
                continue
            if amount < 0:
                if amount < -124:
                    continue
                amount += 256
            histories.append({"ItemAmount": amount, "SilverAmount": _as_int(sv) or 0,
                              "Timestamp": _as_int(ts) or 0})
        if not histories:
            return
        histories.sort(key=lambda h: h["Timestamp"], reverse=True)
        self.stats["history_batches"] += 1
        self.stats["last_data_at"] = self.clock()
        self._emit("markethistories.ingest", {
            "AlbionId": info[0], "LocationId": self.location, "QualityLevel": info[1],
            "Timescale": info[2], "MarketHistories": histories})

    def _emit(self, topic: str, payload: dict) -> None:
        try:
            saved = self.sink(topic, payload)
            log.info("Сохранено %s: %s (локация %s)", topic, saved, self.location)
        except Exception:  # pragma: no cover - не роняем захват из-за ошибки записи
            log.exception("Ошибка сохранения %s", topic)
