"""Задачи бота: сбор ресурсов, рынок, перевозка между городами, данж.

Методы подмешиваются в :class:`albion_trader.bots.Bot` и пользуются его
движением (``walk_to``, ``walk_path``, ``travel_to``), кликами и макросами.
"""

from __future__ import annotations

import math

from .bot_core import BotError, market_price, parse_skills, split_items

RES_KINDS = ("wood", "rock", "fiber", "hide", "ore")
NEAR_NODE = 3.5           # с какого расстояния кликать по самому узлу, м
HARVEST_IDLE = 12.0       # узел не меняется столько секунд — сбор не идёт
FAILED_KEEP = 300.0       # не возвращаться к узлу, который не удалось собрать, с
FIGHT_TIMEOUT = 45.0      # моб не умирает так долго — бросаем
EXPLORE_CELL = 10.0       # клетка «уже были здесь» при разведке данжа, м

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
                "explore_min": 4, "max_min": 40},
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
            self.wait(self.rng.uniform(lo, hi) * 60)

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

    # --- данж ---------------------------------------------------------------
    def dungeon(self) -> None:
        d = self.task_cfg("dungeon")
        try:
            skills = parse_skills(d.get("skills"))
        except ValueError as e:
            raise BotError(f"данж: умения — {e}") from None
        self.home = self.pos()
        self.cooldowns: dict[str, float] = {}
        self.potion_at = -1e9
        visited: set = set()
        done: set = set()
        started = last_found = self.clock()
        while True:
            self.check()
            now = self.clock()
            if now - started > float(d.get("max_min") or 40) * 60:
                self.note("время на данж вышло")
                return
            self.mark_visited(visited)
            self.heal(d)
            mob = self.pick_mob(d, done)
            if mob is not None:
                self.fight(mob, d, skills, done)
                last_found = self.clock()
                continue
            loot = self.pick_loot(d, done)
            if loot is not None:
                self.take_loot(loot, d, done)
                last_found = self.clock()
                continue
            if self.clock() - last_found > float(d.get("explore_min") or 4) * 60:
                self.note(f"больше ничего не найдено — данж пройден (убито: {self.kills}, "
                          f"добыча: {self.loots})")
                return
            self.explore(visited)

    def heal(self, d: dict) -> None:
        hp = self.feed.hp_pct
        if hp is None:
            return
        if d.get("potion_key") and hp < float(d.get("potion_hp") or 40) and self.clock() - self.potion_at > 30:
            self.press_key(d["potion_key"])
            self.potion_at = self.clock()
            self.note(f"здоровье {hp:.0f}% — зелье")
        if hp < float(d.get("retreat_hp") or 20):
            self.status = f"здоровье {hp:.0f}% — отход к входу"
            self.note(self.status)
            if self.home:
                self.walk_to(*self.home, tol=3)
            start = self.clock()
            while (self.feed.hp_pct or 100) < 90 and self.clock() - start < 90:
                self.wait(2)

    def pick_mob(self, d: dict, done: set):
        px, py = self.pos()
        rng = float(d.get("attack_range") or 15)
        mobs = [e for e in self.feed.entities("mob") if e.id not in done and (e.health is None or e.health > 0)]
        near = [m for m in mobs if math.hypot(m.x - px, m.y - py) <= rng]
        pool = near or [m for m in mobs if math.hypot(m.x - px, m.y - py) <= rng * 3]
        return min(pool, key=lambda m: math.hypot(m.x - px, m.y - py)) if pool else None

    def fight(self, mob, d: dict, skills: list, done: set) -> None:
        name = mob.name or f"моб {mob.id}"
        self.status = f"бой: {name}"
        last_click = -1e9
        start = self.clock()
        while True:
            self.check()
            cur = self.feed.entity(mob.id)
            if cur is None or (cur.health is not None and cur.health <= 0):
                done.add(mob.id)
                self.kills += 1
                self.note(f"побеждён {name} (всего {self.kills})")
                self.wait(self.rng.uniform(0.8, 1.6))      # добыча появляется не сразу
                return
            if self.clock() - start > FIGHT_TIMEOUT:
                done.add(mob.id)
                self.note(f"{name}: бой затянулся — пропускаю")
                return
            px, py = self.pos()
            if self.clock() - last_click > 2.5:
                # Клик по мобу — атака (и подход, если далеко).
                self.click_world(cur.x - px, cur.y - py, step=0.6, keep_clear_of=False)
                last_click = self.clock()
            for key, cd in skills:
                if self.clock() >= self.cooldowns.get(key, 0):
                    self.press_key(key)
                    self.cooldowns[key] = self.clock() + cd
                    break
            self.heal(d)
            self.wait(0.6)

    def pick_loot(self, d: dict, done: set):
        px, py = self.pos()
        out = []
        for e in self.feed.entities("loot"):
            if e.id in done or e.opened:
                continue
            chest = "chest" in e.event
            if (chest and not d.get("open_chests")) or (not chest and not d.get("loot_bags")):
                continue
            if e.event == "new_silver_object":
                continue          # серебро подбирается само
            out.append(e)
        return min(out, key=lambda e: math.hypot(e.x - px, e.y - py)) if out else None

    def take_loot(self, e, d: dict, done: set) -> None:
        chest = "chest" in e.event
        what = "сундук" if chest else "добыча"
        self.status = f"к {what}у" if chest else "к добыче"
        done.add(e.id)
        if not self.walk_to(e.x, e.y, tol=2.5, max_steps=25):
            self.note(f"{what}: не дошёл — пропускаю")
            return
        px, py = self.pos()
        self.click_world(e.x - px, e.y - py, step=0.5, keep_clear_of=False)
        self.status = f"открывает {what}"
        self.wait(float(d.get("chest_wait") or 8) if chest else 1.2)
        self.run_template("loot_all", {})
        self.loots += 1
        self.note(f"{what} собрана (всего: {self.loots})" if not chest else f"сундук открыт (всего: {self.loots})")

    def mark_visited(self, visited: set) -> None:
        px, py = self.pos()
        visited.add((int(px // EXPLORE_CELL), int(py // EXPLORE_CELL)))

    def explore(self, visited: set) -> None:
        """Идти туда, где ещё не были (и где проходимо по схеме зоны, если она есть)."""
        px, py = self.pos()
        grid = self.manager.zone_grid(self.feed.zone)
        best, best_score = None, -1e9
        for i in range(12):
            ang = i * math.pi / 6 + self.rng.uniform(-0.2, 0.2)
            tx, ty = px + 22 * math.cos(ang), py + 22 * math.sin(ang)
            if grid is not None and not grid.free_at(tx, ty):
                continue
            cell = (int(tx // EXPLORE_CELL), int(ty // EXPLORE_CELL))
            score = (0 if cell in visited else 10) + self.rng.uniform(0, 3)
            if score > best_score:
                best, best_score = (tx, ty), score
        self.status = "разведка"
        if best is None:
            self.wander(20)
            return
        self.walk_to(*best, tol=3, max_steps=8)
