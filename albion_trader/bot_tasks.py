"""Задачи бота: сбор ресурсов, рынок, перевозка между городами, данж.

Методы подмешиваются в :class:`albion_trader.bots.Bot` и пользуются его
движением (``walk_to``, ``walk_path``, ``travel_to``), кликами и макросами.
"""

from __future__ import annotations

import math

from .bot_core import BotError, market_price, split_items

RES_KINDS = ("wood", "rock", "fiber", "hide", "ore")
NEAR_NODE = 3.5           # с какого расстояния кликать по самому узлу, м
HARVEST_IDLE = 12.0       # узел не меняется столько секунд — сбор не идёт
FAILED_KEEP = 300.0       # не возвращаться к узлу, который не удалось собрать, с

DEFAULTS = {
    "gather": {"res": list(RES_KINDS), "tier_min": 2, "tier_max": 8, "enchant_min": 0,
               "radius": 80, "avoid_players": True, "avoid_mobs": 0, "max_nodes": 0},
    "market": {"place": "", "items": "", "side": "sell", "undercut": 1, "qty": 1,
               "interval_min": 2.0, "interval_max": 6.0, "orders": 0, "macro": ""},
    "transport": {"load_place": "", "unload_place": "", "load_macro": "", "unload": "macro",
                  "unload_macro": "", "sell_items": "", "undercut": 1, "qty": 1,
                  "safety": "safe", "mount_key": "", "round_trip": True, "trips": 0},
    "dungeon": {"skills": "q:3 w:10 e:20", "attack_range": 15, "potion_key": "", "potion_hp": 40,
                "retreat_hp": 20, "loot_bags": True, "open_chests": True, "chest_wait": 8,
                "explore_min": 4, "max_min": 40, "max_floors": 8,
                # Цикл из города: где сдавать добычу, где искать порталы, от кого уходить.
                "exit_key": "", "exit_channel": 10,
                "home_place": "", "deposit_macro": "", "safety": "yellow", "search_zones": 4,
                "search_min": 8, "portal_kinds": ["solo"], "avoid_players": True, "player_radius": 45,
                "runs": 0},
}


class TasksMixin:
    # --- сбор ресурсов ------------------------------------------------------
    def danger(self, g: dict) -> str:
        if not g.get("avoid_players"):
            return ""
        px, py = self.pos()
        for e in self.feed.entities("player"):
            if e.faction == 255 and math.hypot(e.x - px, e.y - py) < 50:
                return e.name or "враждебный игрок"
        return ""

    def pick_node(self, g: dict):
        px, py = self.pos()
        hx, hy = self.home or (px, py)
        now = self.clock()
        mobs = self.feed.entities("mob") if g.get("avoid_mobs") else []
        best, best_d = None, 1e18
        for e in self.feed.entities("resource"):
            if e.res not in (g.get("res") or RES_KINDS) or e.size == 0:
                continue
            if not (int(g.get("tier_min", 1)) <= (e.tier or 0) <= int(g.get("tier_max", 8))):
                continue
            if (e.enchant or 0) < int(g.get("enchant_min", 0)):
                continue
            if self.failed.get(e.id, 0) > now:
                continue
            if math.hypot(e.x - hx, e.y - hy) > float(g.get("radius", 80)):
                continue
            if any(math.hypot(m.x - e.x, m.y - e.y) < float(g["avoid_mobs"]) for m in mobs):
                continue
            d = math.hypot(e.x - px, e.y - py)
            if d < best_d:
                best, best_d = e, d
        return best

    def gather_session(self, until: float, wander_only: bool = False) -> None:
        g = self.task_cfg("gather")
        while self.clock() < until:
            self.check()
            zone = self.feed.zone
            if zone and zone != self.home_zone:
                self.home, self.home_zone = self.pos(), zone
                self.note(f"сменилась зона ({zone}) — точка старта теперь здесь")
            if self.fails_in_row >= 3:
                raise BotError("три узла подряд не собираются — сумка полна, нет нужного инструмента "
                               "или тир ресурса выше навыка")
            if g.get("max_nodes") and self.gathered >= int(g["max_nodes"]):
                if not wander_only:
                    self.note(f"собрано узлов: {self.gathered} — норма выполнена, дальше прогулка")
                wander_only = True
            enemy = self.danger(g)
            if enemy:
                self.status = f"рядом враг: {enemy} — отход"
                self.note(self.status)
                if self.home:
                    self.walk_to(*self.home, tol=4)
                self.wait(self.rng.uniform(8, 15))
                continue
            node = None if wander_only else self.pick_node(g)
            if node is None:
                self.wander(float(g.get("radius", 80)))
                continue
            self.harvest(node)

    def wander(self, radius: float) -> None:
        hx, hy = self.home or self.pos()
        r = radius * 0.6
        ang, dist = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(5, max(6.0, r))
        self.status = "прогулка"
        self.walk_to(hx + dist * math.cos(ang), hy + dist * math.sin(ang), tol=3, max_steps=15)
        self.wait(self.rng.uniform(2, 8))

    def harvest(self, node) -> bool:
        name = f"{node.res} T{node.tier}" + (f".{node.enchant}" if node.enchant else "")
        self.status = f"к ресурсу {name}"
        if not self.walk_to(node.x, node.y, tol=NEAR_NODE, target=node.id):
            self.failed[node.id] = self.clock() + FAILED_KEEP
            self.note(f"не дошёл до {name} — пропускаю")
            return False
        px, py = self.pos()
        before_harvests = self.feed.harvests
        self.click_world(node.x - px, node.y - py, step=0.5, keep_clear_of=False)
        self.status = f"сбор {name}"
        size = node.size
        changed = self.clock()
        while True:
            self.wait(0.5)
            cur = self.feed.entity(node.id)
            if cur is None or cur.size == 0:
                break
            if cur.size != size:
                size, changed = cur.size, self.clock()
            if self.feed.harvests != before_harvests:
                before_harvests, changed = self.feed.harvests, self.clock()
            if self.clock() - changed > HARVEST_IDLE:
                self.failed[node.id] = self.clock() + FAILED_KEEP
                self.fails_in_row += 1
                self.note(f"{name}: сбор не идёт (нет инструмента, мал тир или мешает объект) — пропускаю")
                return False
        self.gathered += 1
        self.fails_in_row = 0
        self.note(f"собран {name} (всего {self.gathered})")
        self.wait(self.rng.uniform(0.5, 2.5))
        return True

    # --- рынок --------------------------------------------------------------
    def place_order(self, item: str, side: str, undercut: float, qty: int, macro: str = "") -> bool:
        """Один заказ: цена из собранных данных рынка, действия — готовый шаблон или свой макрос."""
        best = self.manager.price_of(item, self.feed.location, side)
        price = market_price(best, side, undercut)
        if price is None:
            self.note(f"{item}: нет цены на этом рынке — пропускаю (откройте рынок в игре, "
                      "чтобы программа собрала цены)")
            return False
        values = {"item": item, "name": self.manager.item_name(item), "price": price, "qty": qty}
        self.status = f"заказ {values['name']} по {price}"
        if macro:
            self.run_macro(macro, values)
        else:
            self.run_template("market_sell" if side == "sell" else "market_buy", values)
        self.orders += 1
        self.note(f"{'продажа' if side == 'sell' else 'покупка'}: {values['name']} × {qty} по {price} "
                  f"(заказов: {self.orders})")
        return True

    def market_session(self, until: float) -> None:
        m = self.task_cfg("market")
        items = split_items(m.get("items"))
        if not items:
            raise BotError("рынок: не задан список предметов")
        if m.get("place"):
            self.go_place(m["place"])
        limit = int(m.get("orders") or 0)
        while self.clock() < until:
            self.check()
            if limit and self.orders >= limit:
                self.note(f"выставлено заказов: {self.orders} — готово")
                return
            self.place_order(self.rng.choice(items), m.get("side", "sell"), float(m.get("undercut") or 0),
                             int(m.get("qty") or 1), m.get("macro") or "")
            lo = float(m.get("interval_min") or 1)
            hi = max(lo, float(m.get("interval_max") or lo))
            self.status = "ждёт до следующего заказа"
            self.wait_idle(self.rng.uniform(lo, hi) * 60)

    # --- перевозка ----------------------------------------------------------
    def transport(self) -> None:
        t = self.task_cfg("transport")
        if not t.get("load_place") or not t.get("unload_place"):
            raise BotError("перевозка: выберите место погрузки (город А) и место разгрузки (город Б)")
        trips = int(t.get("trips") or 0)
        done = 0
        while True:
            self.check()
            self.status = "в пути к погрузке"
            self.go_place(t["load_place"], t.get("safety", "safe"))
            if t.get("load_macro"):
                self.status = "погрузка"
                self.run_macro(t["load_macro"], {})
            self.mount(t)
            self.status = "в пути к разгрузке"
            self.go_place(t["unload_place"], t.get("safety", "safe"))
            self.status = "разгрузка"
            if t.get("unload") == "market_sell":
                items = split_items(t.get("sell_items"))
                if not items:
                    raise BotError("перевозка: для продажи в городе Б задайте список предметов")
                for item in items:
                    self.check()
                    self.place_order(item, "sell", float(t.get("undercut") or 0), int(t.get("qty") or 1))
                    self.wait(self.rng.uniform(1.5, 4))
            elif t.get("unload_macro"):
                self.run_macro(t["unload_macro"], {})
            done += 1
            self.trips = done
            self.note(f"рейс {done} завершён")
            if not t.get("round_trip") or (trips and done >= trips):
                return
            self.mount(t)

    def mount(self, t: dict) -> None:
        if t.get("mount_key"):
            self.press_key(t["mount_key"])
            self.wait(3.5)        # посадка на маунта
