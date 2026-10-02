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
               "radius": 80, "avoid_players": True, "avoid_mobs": 0, "max_nodes": 0,
               # Тепловая карта узлов (база радара) — куда идти, когда рядом пусто.
               "use_heat": True, "respawn_min": 10,
               # Полная сумка — отнести домой и вернуться.
               "bag_slots": 0, "home_place": "", "deposit_macro": ""},
    "market": {"place": "", "items": "", "side": "sell", "undercut": 1, "qty": 1,
               "interval_min": 2.0, "interval_max": 6.0, "orders": 0, "macro": "",
               # Свои заказы (из трафика): не дублировать, перебитые — переставить; бюджет покупок.
               "skip_own": True, "relist_outbid": False, "budget": 0},
    "transport": {"load_place": "", "unload_place": "", "load_macro": "", "unload": "macro",
                  "unload_macro": "", "sell_items": "", "undercut": 1, "qty": 1,
                  "safety": "safe", "mount_key": "", "round_trip": True, "trips": 0,
                  # Игроки на пути — убежать и перестроить маршрут в обход этой зоны.
                  "avoid_players": True, "player_radius": 50, "friends": "", "danger_ip": 0,
                  "escape_key": "", "avoid_min": 15},
    "dungeon": {"skills": "q:3 w:10 e:20", "attack_range": 15, "potion_key": "", "potion_hp": 40,
                "retreat_hp": 20, "loot_bags": True, "open_chests": True, "chest_wait": 8,
                "explore_min": 4, "max_min": 40, "max_floors": 8,
                # Цикл из города: где сдавать добычу, где искать порталы, от кого уходить.
                "exit_key": "", "exit_channel": 10, "retreat_exit": False,
                # Бой: профиль умений (условия @), кайт, безопасность.
                "kite": False, "kite_dist": 5, "max_pack": 3, "rest_hp": 60, "max_mob_tier": 0,
                "skip_elite": False,
                # Угрозы: друзья не в счёт, сила снаряжения, клавиши побега.
                "friends": "", "danger_ip": 0, "escape_key": "", "mount_key": "",
                "portal_enchant_min": 0, "portal_enchant_max": 4,
                # Сумка и обслуживание.
                "bag_slots": 0, "repair_place": "", "repair_macro": "", "repair_every": 0,
                "restock_place": "", "restock_macro": "", "restock_every": 0,
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
            # Узел рядом с другими подходящими выгоднее: следующий будет близко.
            neighbours = sum(1 for o in self.feed.entities("resource")
                             if o.id != e.id and o.res == e.res and math.hypot(o.x - e.x, o.y - e.y) < 15)
            score = d - 4 * min(neighbours, 3) - 3 * (e.enchant or 0)
            if score < best_d:
                best, best_d = e, score
        return best

    def heat_target(self, g: dict):
        """Куда идти, когда рядом пусто: клетка тепловой карты ресурсов (база радара) с
        подходящими узлами, где давно не были (узлы успели восстановиться)."""
        if not g.get("use_heat"):
            return None
        zone = self.feed.zone
        cells = self.manager.heat(zone) if zone else []
        px, py = self.pos()
        hx, hy = self.home or (px, py)
        now = self.clock()
        respawn = float(g.get("respawn_min") or 10) * 60
        visited = self.heat_visited.setdefault(zone, {})
        best, best_score = None, -1e18
        for c in cells:
            if c["res"] not in (g.get("res") or RES_KINDS):
                continue
            if not int(g.get("tier_min", 1)) <= c["tier"] <= int(g.get("tier_max", 8)):
                continue
            if c["enchant"] < int(g.get("enchant_min", 0)):
                continue
            if math.hypot(c["x"] - hx, c["y"] - hy) > float(g.get("radius", 80)):
                continue
            key = (round(c["x"]), round(c["y"]))
            if now - visited.get(key, -1e18) < respawn:
                continue
            if math.hypot(c["x"] - px, c["y"] - py) < 12:
                visited[key] = now
                continue
            score = c["seen"] / (math.hypot(c["x"] - px, c["y"] - py) + 20)
            if score > best_score:
                best, best_score = c, score
        return best

    def gather_bag_full(self, g: dict) -> bool:
        slots = int(g.get("bag_slots") or 0)
        return self.feed.overloaded or bool(slots and self.feed.items_put - self.items_base >= slots)

    def unload_gathering(self, g: dict) -> None:
        """Сумка полна — отнести домой, сдать и вернуться на место сбора."""
        if not g.get("home_place"):
            raise BotError("сумка полна, а место сдачи не задано — выберите «куда сдавать» в настройках сбора")
        back_zone, back = self.feed.zone, self.pos()
        self.note("сумка полна — несу домой")
        self.go_place(g["home_place"], "yellow")
        self.deposit({"deposit_macro": g.get("deposit_macro")})
        self.status = "возвращается к сбору"
        self.travel_to(back_zone, back[0], back[1], safety="yellow")

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
            if self.gather_bag_full(g):
                self.unload_gathering(g)
                continue
            node = None if wander_only else self.pick_node(g)
            if node is None:
                cell = None if wander_only else self.heat_target(g)
                if cell is not None:
                    self.status = f"к месту, где бывают ресурсы ({cell['res']} T{cell['tier']})"
                    self.walk_path(cell["x"], cell["y"])
                    self.heat_visited.setdefault(self.feed.zone, {})[(round(cell["x"]), round(cell["y"]))] = \
                        self.clock()
                    continue
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
    def place_order(self, item: str, side: str, undercut: float, qty: int, macro: str = "",
                    m: dict | None = None) -> bool:
        """Один заказ: цена из собранных данных рынка, действия — готовый шаблон или свой макрос.

        ``m`` — настройки рынка: свои заказы (не дублировать, перебитые переставлять) и бюджет."""
        m = m or {}
        if m.get("skip_own", True) or m.get("relist_outbid"):
            mine = self.manager.my_orders(item, self.feed.location, side)
            if mine:
                outbid = all(o.get("outbid") for o in mine)
                if not outbid and m.get("skip_own", True):
                    self.note(f"{item}: ваш заказ уже лучший — не дублирую")
                    return False
                if outbid and not m.get("relist_outbid"):
                    self.note(f"{item}: ваш заказ перебит (переставление выключено) — пропускаю")
                    return False
                if outbid:
                    self.note(f"{item}: ваш заказ перебит — ставлю новый")
        best = self.manager.price_of(item, self.feed.location, side)
        price = market_price(best, side, undercut)
        if price is None:
            self.note(f"{item}: нет цены на этом рынке — пропускаю (откройте рынок в игре, "
                      "чтобы программа собрала цены)")
            return False
        budget = float(m.get("budget") or 0)
        if side == "buy" and budget and self.spent + price * qty > budget:
            raise BotError(f"бюджет покупок исчерпан: потрачено {self.spent:,.0f} из {budget:,.0f}".replace(",", " "))
        values = {"item": item, "name": self.manager.item_name(item), "price": price, "qty": qty}
        self.status = f"заказ {values['name']} по {price}"
        if macro:
            self.run_macro(macro, values)
        else:
            self.run_template("market_sell" if side == "sell" else "market_buy", values)
        self.orders += 1
        if side == "buy":
            self.spent += price * qty
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
                             int(m.get("qty") or 1), m.get("macro") or "", m)
            lo = float(m.get("interval_min") or 1)
            hi = max(lo, float(m.get("interval_max") or lo))
            self.status = "ждёт до следующего заказа"
            self.wait_idle(self.rng.uniform(lo, hi) * 60)

    # --- перевозка ----------------------------------------------------------
    def transport(self) -> None:
        t = self.task_cfg("transport")
        self.route_guard = t
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
                self.wait(1.0)
                if self.feed.overloaded:
                    self.note("перегруз после погрузки — маунт пойдёт медленнее, возьмите меньше")
                    self.alert(f"overload:{done}", "Бот: перегруз", "после погрузки персонаж перегружен")
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
