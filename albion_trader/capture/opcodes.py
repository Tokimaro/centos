"""Актуальные номера операций и событий из исходников albiondata-client.

После обновлений игры номера сдвигаются; сообщество AODP быстро обновляет
``client/operations.go`` и ``client/events.go``. Команда ``update-opcodes``
скачивает их (только чтение открытого репозитория) и пишет ``data/opcodes.json``.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

BASE_URL = "https://raw.githubusercontent.com/ao-data/albiondata-client/master/client/"

OPERATION_NAMES = {
    "join": "opJoin",
    "get_game_server_by_cluster": "opGetGameServerByCluster",
    "auction_get_offers": "opAuctionGetOffers",
    "auction_get_requests": "opAuctionGetRequests",
    "auction_buy_offer": "opAuctionBuyOffer",
    "auction_sell_request": "opAuctionSellRequest",
    "auction_get_finished": "opAuctionGetFinishedAuctions",
    "auction_get_my_offers": "opAuctionGetMyOpenOffers",
    "auction_get_my_requests": "opAuctionGetMyOpenRequests",
    "auction_get_my_auctions": "opAuctionGetMyOpenAuctions",
    "auction_get_item_average_stats": "opAuctionGetItemAverageStats",
    "auction_sell_specific_item": "opAuctionSellSpecificItemRequest",
    "get_mail_infos": "opGetMailInfos",
    "read_mail": "opReadMail",
    "gold_market_get_average_info": "opGoldMarketGetAverageInfo",
}
EVENT_NAMES = {
    "take_silver": "evTakeSilver",
    "update_money": "evUpdateMoney",
    "update_fame": "evUpdateFame",
    "update_respec_points": "evUpdateReSpecPoints",
    "new_loot": "evNewLoot",
    "character_stats": "evCharacterStats",
    "killed_player": "evKilledPlayer",
    "died": "evDied",
    "other_grabbed_loot": "evOtherGrabbedLoot",
    "harvest_finished": "evHarvestFinished",
    "craft_item_finished": "evCraftItemFinished",
    "fishing_finished": "evFishingFinished",
    "new_loot_chest": "evNewLootChest",
    "loot_chest_opened": "evLootChestOpened",
    "redzone_world_map_event": "evRedZoneWorldMapEvent",
    "festivities_update": "evFestivitiesUpdate",
}

_START = re.compile(r"^(\w+)\s+\w+Type\s*=\s*iota\s*$")
_ENTRY = re.compile(r"^(\w+)(?:\s*=\s*(\d+))?\s*$")


def parse_go_enum(source: str, prefix: str) -> dict[str, int]:
    """Разбор Go-перечисления вида ``opUnused OperationType = iota`` / ``opPing`` / …"""
    values: dict[str, int] = {}
    n = None
    for raw in source.splitlines():
        line = raw.split("//", 1)[0].strip()
        if n is None:
            m = _START.match(line)
            if m and m.group(1).startswith(prefix):
                n = 0
                values[m.group(1)] = 0
            continue
        if line == ")":
            break
        m = _ENTRY.match(line)
        if m and m.group(1).startswith(prefix):
            n = int(m.group(2)) if m.group(2) else n + 1
            values[m.group(1)] = n
    return values


def build_opcodes(operations_go: str, events_go: str) -> dict:
    ops = parse_go_enum(operations_go, "op")
    evs = parse_go_enum(events_go, "ev")
    out = {k: ops[v] for k, v in OPERATION_NAMES.items() if v in ops}
    out["events"] = {k: evs[v] for k, v in EVENT_NAMES.items() if v in evs}
    missing = [v for v in OPERATION_NAMES.values() if v not in ops] + \
              [v for v in EVENT_NAMES.values() if v not in evs]
    if len(out) - 1 < len(OPERATION_NAMES) // 2:
        raise ValueError("не удалось разобрать operations.go — формат изменился?")
    out["_missing"] = missing
    return out


def update(path: str | Path, base_url: str = BASE_URL) -> dict:
    def fetch(name):
        with urllib.request.urlopen(base_url + name, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    codes = build_opcodes(fetch("operations.go"), fetch("events.go"))
    missing = codes.pop("_missing")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(codes, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"codes": codes, "missing": missing}
