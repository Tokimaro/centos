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
    "auction_sell_request": 88,
    "auction_get_finished": 89,
    "auction_get_my_offers": 92,
    "auction_get_my_requests": 93,
    "auction_get_my_auctions": 94,
    "auction_get_item_average_stats": 95,
    "auction_sell_specific_item": 315,
    "get_mail_infos": 174,
    "read_mail": 176,
    "gold_market_get_average_info": 250,
    "move": 21,                   # свой запрос движения (позиция для радара)
}

# Номера событий из client/events.go (сверены со StatisticsAnalysisTool).
DEFAULT_EVENTS = {
    "harvest_finished": 61,
    "take_silver": 62,
    "craft_item_finished": 71,
    "update_money": 81,
    "update_fame": 82,
    "update_respec_points": 84,
    "new_loot": 98,
    "character_stats": 143,
    "killed_player": 164,
    "died": 165,
    "other_grabbed_loot": 279,
    "fishing_finished": 358,
    "new_loot_chest": 393,
    "loot_chest_opened": 395,
    "redzone_world_map_event": 480,
    "festivities_update": 519,
    # Радар: объекты вокруг персонажа (radar.py). Номера из того же events.go.
    "leave": 1,
    "move": 3,
    "teleport": 4,
    "health_update": 6,
    "new_character": 29,
    "new_simple_harvestable_object_list": 39,
    "new_harvestable_object": 40,
    "new_silver_object": 44,
    "harvestable_change_state": 46,
    "mob_change_state": 47,
    "new_mob": 123,
    "new_treasure_chest": 117,
    # Сведения об игроках (как в ZQRadar): смена снаряжения, регенерация HP,
    # маунт, PvP-флаг. Номера до ~165 совпадают с ZQRadar, дальше сдвинуты на
    # 5–6 — уточняются командой update-opcodes по именам из events.go.
    "character_equipment_changed": 90,
    "regeneration_health_changed": 91,
    "mounted": 209,
    "change_flagging_finished": 365,
}
EVENT_MOVE = 3  # самое частое событие (движение) — разбираем, только пока открыт радар

HISTORY_CACHE_SIZE = 1024
ENCRYPTION_WINDOW = 3.0
MARKET_RESPONSE_TIMEOUT = 15.0

_RE_ISLAND = re.compile(r"(?i)@island@[0-9a-f-]{36}")
_RE_NUMERIC = re.compile(r"^[0-9]{3,6}$")


def load_opcodes(path: str | Path | None) -> dict:
    """Коды операций и событий (``{"events": {...}}``) с переопределениями из файла."""
    codes = dict(DEFAULT_OPCODES)
    events = dict(DEFAULT_EVENTS)
    if path and Path(path).exists():
        try:
            override = json.loads(Path(path).read_text(encoding="utf-8"))
            codes.update({k: int(v) for k, v in override.items() if k in codes})
            events.update({k: int(v) for k, v in (override.get("events") or {}).items() if k in events})
            log.info("Коды операций переопределены из %s", path)
        except (ValueError, TypeError, OSError, AttributeError) as e:
            log.warning("Не удалось прочитать %s: %s", path, e)
    codes["events"] = events
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


_RE_ZONE = re.compile(r"^[\w\-#@.]{1,80}$")


def normalize_zone(value) -> str:
    """Любая зона игры: город (``3004``), Дорога Авалона (``TNL-001``), данж и т. п.

    Переходы приходят как ``<uuid>@<кластер>`` — берём кластер. Острова
    нормализуются так же, как у рынка.
    """
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    island = normalize_location_id(s)
    if island.startswith("@ISLAND@"):
        return island
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    return s if _RE_ZONE.match(s) else ""


def is_market_location(loc: str) -> bool:
    return bool(loc) and (loc.isdigit() or loc.startswith("BLACKBANK-")
                          or loc.endswith(("-HellDen", "-Auction2")))


def _loads(s):
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _looks_like_market_orders(orders: list) -> bool:
    if not orders:
        return False
    first = _loads(orders[0])
    return bool(first) and "AuctionType" in first and "UnitPriceSilver" in first and "ItemTypeId" in first


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
        self._listeners: dict[str, list[Callable]] = {}
        self.set_opcodes(opcodes)
        self.character_name = ""
        self.clock = clock
        self.lock = threading.Lock()
        self.location = ""
        self.zone = ""              # любая текущая зона (не только рынок)
        self.object_id: int | None = None   # id объекта своего персонажа (Join[0])
        self.history_lookup: dict[int, tuple[int, int, int]] = {}
        self.last_market_request = 0.0
        # Время отправки запросов рынка, на которые ещё не пришёл ответ.
        # Игра шлёт по 4 запроса на один просмотр, поэтому считаем штучно.
        self.pending_market_requests: list[float] = []
        # До какого времени разбирать события движения (их больше всего) —
        # радар продлевает срок, пока его смотрят.
        self.moves_until = 0.0
        self._last_location_warning = 0.0
        self.stats = {
            "location": "", "zone": "", "orders": 0, "order_batches": 0, "history_batches": 0,
            "last_data_at": None, "encrypted_at": None, "no_location_drops": 0,
            "market_requests": 0, "market_responses_lost": 0,
            "character": "", "events": 0,
            # Заказы, опознанные только по содержимому (код операции неизвестен) — признак
            # того, что после патча сместились номера операций.
            "orders_by_content": 0, "orders_by_content_at": None,
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

    def set_opcodes(self, opcodes: dict | None) -> None:
        """Применяет коды операций/событий (в том числе на лету после update-opcodes)."""
        opcodes = dict(opcodes or DEFAULT_OPCODES)
        self.ev = {**DEFAULT_EVENTS, **(opcodes.pop("events", None) or {})}
        self.op = {**DEFAULT_OPCODES, **opcodes}
        self._interesting = set(self.op.values())
        self._event_names = {v: k for k, v in self.ev.items()}
        self._op_names = {v: k for k, v in self.op.items()}

    # --- подписчики ---------------------------------------------------
    def on(self, name: str, fn: Callable) -> None:
        """Подписка: ``request:<операция>``, ``response:<операция>``, ``event:<событие>``,
        ``event`` (код, параметры) — любое событие, ``my_orders`` (kind, orders), ``location`` (рынок), ``zone`` (зона, предыдущая зона),
        ``character`` (имя)."""
        self._listeners.setdefault(name, []).append(fn)

    def _fire(self, name: str, *args) -> None:
        for fn in self._listeners.get(name, ()):
            try:
                fn(*args)
            except Exception:  # pragma: no cover - ошибка подписчика не роняет захват
                log.exception("Ошибка обработчика %s", name)

    @staticmethod
    def wants_event(code: int) -> bool:
        return code != EVENT_MOVE

    def accepts_event(self, code: int) -> bool:
        """Фильтр парсера: движение пропускается, пока радар выключен."""
        return code != EVENT_MOVE or self.clock() < self.moves_until

    # --- колбэки парсера -----------------------------------------------
    def on_request(self, op_code: int, params: dict) -> None:
        code = self._code(params, op_code)
        name = self._op_names.get(code)
        if name:
            with self.lock:
                self._fire("request:" + name, params)
        with self.lock:
            if code == self.op["get_game_server_by_cluster"]:
                self._set_location(params.get(0), "GetGameServerByCluster")
                self._set_zone(params.get(0))
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
            own_kind = self._own_orders_kind(code, params, orders if is_orders else None)
            if own_kind:
                self._my_orders(own_kind, orders)
            elif is_orders and (code in (self.op["auction_get_offers"], self.op["auction_get_requests"],
                                         self.op["auction_buy_offer"]) or set(params) == {0}):
                self._market_orders(orders)
            elif is_orders and _looks_like_market_orders(orders):
                # Код операции неизвестен, но это явно заказы рынка — сохраняем и отмечаем.
                self.stats["orders_by_content"] += 1
                self.stats["orders_by_content_at"] = self.clock()
                self._market_orders(orders)
            elif code == self.op["join"]:
                self._set_object_id(params.get(0))
                self._set_location(params.get(8), "Join")
                self._set_zone(params.get(8))
                self._set_character(params.get(2))
            elif code == self.op["get_game_server_by_cluster"]:
                self._set_location(params.get(0), "GetGameServerByCluster")
                self._set_zone(params.get(0))
            elif code == self.op["auction_get_item_average_stats"]:
                self._history_response(params)
            elif code not in self._interesting and normalize_location_id(params.get(8)):
                # После обновлений игры код Join может сместиться — узнаём его по форме.
                self._set_object_id(params.get(0))
                self._set_location(params.get(8), "Join?")
                self._set_zone(params.get(8))
                self._set_character(params.get(2))
            name = self._op_names.get(code)
            if name:
                self._fire("response:" + name, params)

    def on_event(self, code: int, params: dict) -> None:
        v = _as_int(params.get(252))
        code = code if v is None else v
        name = self._event_names.get(code)
        if self._listeners.get("event"):
            with self.lock:
                self._fire("event", code, params)
        if not name:
            return
        with self.lock:
            self.stats["events"] += 1
            self._fire("event:" + name, params)

    # --- мои заказы -----------------------------------------------------
    def _own_orders_kind(self, code: int, params: dict, orders) -> str | None:
        own_codes = {
            self.op["auction_get_my_offers"]: "offers",
            self.op["auction_get_my_requests"]: "requests",
            self.op["auction_get_my_auctions"]: "auctions",
            self.op["auction_get_finished"]: "finished",
        }
        if code in own_codes:
            return own_codes[code] if isinstance(params.get(0), list) else None
        if orders is None or code in (self.op["auction_get_offers"], self.op["auction_get_requests"]):
            return None
        # Коды могли сместиться после патча: список, где все заказы — ваши, это «мои заказы».
        if self.character_name and orders:
            parsed = [_loads(o) for o in orders]
            if all(p and self.character_name in (p.get("SellerName"), p.get("BuyerName")) for p in parsed):
                return "mine"
        return None

    def _my_orders(self, kind: str, raw: list) -> None:
        orders = [o for o in (_loads(x) for x in raw) if o]
        self._fire("my_orders", kind, orders)

    def _set_character(self, name) -> None:
        if isinstance(name, str) and name and name != self.character_name:
            self.character_name = name
            self.stats["character"] = name
            log.info("Персонаж: %s", name)
            self._fire("character", name)

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
            self._fire("location", loc)

    def _set_zone(self, raw) -> None:
        zone = normalize_zone(raw)
        if zone and zone != self.zone:
            prev, self.zone = self.zone, zone
            self.stats["zone"] = zone
            self._fire("zone", zone, prev)

    def _set_object_id(self, raw) -> None:
        oid = _as_int(raw)
        if oid is not None and not isinstance(raw, bool):
            self.object_id = oid

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
