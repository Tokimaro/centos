"""Точный учёт славы и серебра за период.

Слава: полная формула игры (база с множителем зоны, премиум, сумка прозрения,
бонус-фактор) и источник — бой, сбор, крафт, рыбалка.

Серебро:
* с мобов — чистыми (после налога кластера и гильдии, штрафа альянса), налоги
  показываются отдельно;
* из лута — серебро, подобранное вами;
* рынок — ваши продажи (после налога) и покупки;
* изменение баланса (UpdateMoney и баланс при входе в зону) и «прочее» —
  разница между изменением баланса и учтённым выше: ремонт, телепорты,
  комиссии, покупка золота и т. п.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict

from .destiny import IDLE_GAP, played_seconds

SOURCES = ("combat", "gathering", "crafting", "fishing")


def _data(e) -> dict:
    try:
        return json.loads(e["data"] or "{}")
    except ValueError:
        return {}


def report(conn, since: int, until: int, character: str, tax: float, session_id: int | None = None) -> dict:
    sql = "SELECT * FROM activity_events WHERE ts >= ? AND ts <= ?"
    args: list = [since, until]
    if session_id:
        sql += " AND session_id = ?"
        args.append(session_id)
    fame = {"total": 0.0, "base": 0.0, "premium": 0.0, "satchel": 0.0, "bonus": 0.0,
            "by_source": {s: 0.0 for s in SOURCES}}
    silver = {"mobs_net": 0.0, "mobs_gross": 0.0, "cluster_tax": 0.0, "guild_tax": 0.0, "alliance_penalty": 0.0,
              "loot": 0.0}
    balances: list[tuple[int, float]] = []
    hourly: dict[int, dict] = defaultdict(lambda: {"fame": 0.0, "silver": 0.0})
    daily: dict[str, dict] = defaultdict(lambda: {"fame": 0.0, "silver": 0.0})
    for e in conn.execute(sql + " ORDER BY ts, id", args):
        e = dict(e)
        hour = e["ts"] // 3600 * 3600
        day = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
        if e["kind"] == "fame":
            d = _data(e)
            v = e["value"] or 0
            fame["total"] += v
            src = d.get("src") if d.get("src") in SOURCES else "combat"
            fame["by_source"][src] += v
            base, prem, sat = d.get("base", v), d.get("premium", 0), d.get("satchel", 0)
            fame["base"] += base
            fame["premium"] += prem
            fame["satchel"] += sat
            fame["bonus"] += v - base - prem - sat
            hourly[hour]["fame"] += v
            daily[day]["fame"] += v
        elif e["kind"] == "silver":
            d = _data(e)
            silver["mobs_net"] += e["value"] or 0
            # У старых записей нет суммы до налогов — тогда она равна чистой.
            silver["mobs_gross"] += e["amount"] if e["amount"] is not None else (e["value"] or 0)
            for k in ("cluster_tax", "guild_tax", "alliance_penalty"):
                silver[k] += d.get(k, 0) or 0
            hourly[hour]["silver"] += e["value"] or 0
            daily[day]["silver"] += e["value"] or 0
        elif e["kind"] == "loot" and (not character or e["actor"] == character) and _data(e).get("silver"):
            silver["loot"] += e["amount"] or 0
            hourly[hour]["silver"] += e["amount"] or 0
            daily[day]["silver"] += e["amount"] or 0
        elif e["kind"] == "balance" and e["value"] is not None:
            balances.append((e["ts"], e["value"]))

    # Рынок — только в пределах наблюдаемого баланса, чтобы «прочее» считалось честно.
    market_from, market_to = (balances[0][0], balances[-1][0]) if len(balances) > 1 else (since, until)
    if session_id:
        s = conn.execute("SELECT started, ended FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if s and len(balances) < 2:
            market_from, market_to = s["started"], s["ended"] or until
    sales = purchases = 0.0
    for t in conn.execute("SELECT kind, total FROM my_trades WHERE ts >= ? AND ts <= ?", (market_from, market_to)):
        if t["kind"] == "sell":
            sales += (t["total"] or 0) * (1 - tax)
        else:
            purchases += t["total"] or 0

    balance = None
    if len(balances) > 1:
        change = balances[-1][1] - balances[0][1]
        # Учтённое между первой и последней отметкой баланса.
        inside = conn.execute(
            f"SELECT kind, value, amount, actor, data FROM activity_events WHERE ts > ? AND ts <= ?"
            f"{' AND session_id = ?' if session_id else ''}",
            [balances[0][0], balances[-1][0]] + ([session_id] if session_id else [])).fetchall()
        mobs = sum(r["value"] or 0 for r in inside if r["kind"] == "silver")
        loot = sum(r["amount"] or 0 for r in inside if r["kind"] == "loot"
                   and (not character or r["actor"] == character) and _data(r).get("silver"))
        explained = mobs + loot + sales - purchases
        balance = {"start": balances[0][1], "end": balances[-1][1], "change": round(change),
                   "explained": round(explained), "other": round(change - explained)}

    seconds = played_seconds(conn, since) if not session_id else _session_seconds(conn, session_id)
    hours = seconds / 3600
    income = silver["mobs_net"] + silver["loot"] + sales
    rnd = lambda d: {k: (round(v) if isinstance(v, float) else v) for k, v in d.items()}
    fame_out = rnd({k: v for k, v in fame.items() if k != "by_source"})
    fame_out["by_source"] = rnd(fame["by_source"])
    return {
        "hours": round(hours, 2), "fame": fame_out, "silver": rnd(silver),
        "market": {"sales_net": round(sales), "purchases": round(purchases)},
        "balance": balance, "income": round(income),
        "fame_per_hour": round(fame["total"] / hours) if hours >= 1 / 60 else None,
        "income_per_hour": round(income / hours) if hours >= 1 / 60 else None,
        "series_bucket": _bucket(hourly),
        "hourly": _series(hourly),
        "daily": [{"day": d, "fame": round(v["fame"]), "silver": round(v["silver"])}
                  for d, v in sorted(daily.items(), reverse=True)],
    }


def _session_seconds(conn, session_id: int) -> int:
    total, prev = 0, None
    for r in conn.execute("SELECT ts FROM activity_events WHERE session_id = ? ORDER BY ts", (session_id,)):
        if prev is not None:
            total += min(max(r["ts"] - prev, 0), IDLE_GAP)
        prev = r["ts"]
    return total


MAX_HOURLY_SPAN = 72 * 3600


def _bucket(hourly: dict) -> int:
    return 3600 if not hourly or max(hourly) - min(hourly) <= MAX_HOURLY_SPAN else 86400


def _series(hourly: dict) -> list[dict]:
    """Ряд для графика без «дыр»: пустые часы (или дни при длинном периоде) — нули."""
    if not hourly:
        return []
    step = _bucket(hourly)
    # Дни — по местной полуночи, как в таблице «по дням».
    off = time.localtime().tm_gmtoff if step == 86400 else 0
    buckets: dict[int, dict] = defaultdict(lambda: {"fame": 0.0, "silver": 0.0})
    for h, v in hourly.items():
        key = (h + off) // step * step - off
        buckets[key]["fame"] += v["fame"]
        buckets[key]["silver"] += v["silver"]
    start, end = min(buckets), max(buckets)
    return [{"ts": t, "fame": round(buckets[t]["fame"]) if t in buckets else 0,
             "silver": round(buckets[t]["silver"]) if t in buckets else 0} for t in range(start, end + step, step)]
