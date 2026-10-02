"""Бот данжей: из города — в ближайшие зоны искать порталы, этажи, босс, сундук, домой.

Цикл («Данжи по кругу»):

1. Из города — в ближайшую зону открытого мира с допустимой опасностью; бегать по
   ней (по схеме зоны — только по проходимому) и искать на радаре вход в случайный
   данж нужного вида (зелёный — соло). Рядом с порталом игроки — не заходить. Не
   нашёл за отведённое время — следующая зона.
2. В данже — этаж за этажом: бой с мобами, добыча и сундуки, разведка; этаж пуст —
   выход на следующий этаж (выход, который не у точки появления). Убит босс и открыт
   сундук после него — данж пройден.
3. Игроки рядом (радар) — уход от контакта: в открытом мире — отойти в сторону от
   них, в данже — бросить данж и выйти тем же путём.
4. Выход из данжа тем же путём (этаж за этажом к точкам появления), дорога в город к
   сундуку, сдача добычи (готовый шаблон по точкам интерфейса или свой макрос) и
   снова п. 1.

Коды событий порталов — из ``events.go`` (``NewRandomDungeonExit``, ``NewExit``…);
вид данжа — по имени из события (SOLO, CORRUPTED, HELLGATE…), как в ZQRadar.
"""

from __future__ import annotations

import math

from .bot_core import BotError, parse_skills, skill_ready
from .bot_threat import DANGER, ThreatTracker, friends_of

FIGHT_TIMEOUT = 45.0      # моб не умирает так долго — бросаем
EXPLORE_CELL = 10.0       # клетка «уже были здесь» при разведке, м
EXIT_EVENTS = ("new_exit", "new_portal_exit", "new_portal_entrance", "new_random_dungeon_exit")
ENTRY_RADIUS = 15.0       # выход ближе к точке появления на этаже — это выход назад
PORTAL_KINDS = {"solo": "соло (зелёные)", "group": "групповые", "corrupted": "проклятые",
                "hellgate": "адские врата", "avalon": "авалонские", "unknown": "без названия"}


class DungeonAbort(Exception):
    """Бросить данж сейчас (мало здоровья — быстрый выход)."""


def portal_kind(name: str) -> str:
    up = (name or "").upper()
    if not up or up in ("ВХОД В ДАНЖ",):
        return "unknown"
    if "CORRUPT" in up:
        return "corrupted"
    if "HELLGATE" in up:
        return "hellgate"
    if "AVALON" in up or "ROADS" in up:
        return "avalon"
    if "SOLO" in up:
        return "solo"
    return "group"


class DungeonMixin:
    # --- общее ----------------------------------------------------------
    def check_alive(self) -> None:
        if self.feed.hp is not None and self.feed.hp <= 0:
            raise BotError("персонаж погиб — бот остановлен")

    def players_near(self, radius: float, x: float | None = None, y: float | None = None) -> list:
        if x is None:
            x, y = self.pos()
        return [e for e in self.feed.entities("player") if math.hypot(e.x - x, e.y - y) <= radius]

    def threats(self, d: dict) -> list:
        if not d.get("avoid_players"):
            return []
        if getattr(self, "threat_tracker", None) is None:
            self.threat_tracker = ThreatTracker()
        return self.threat_tracker.assess(self.feed, self.pos(), d)

    def away_point(self, threats: list, dist: float) -> tuple[float, float] | None:
        px, py = self.pos()
        vx = vy = 0.0
        for t in threats:
            e = t.entity
            r = max(1.0, math.hypot(e.x - px, e.y - py))
            w = 2.0 if t.level >= DANGER else 1.0
            vx, vy = vx + w * (px - e.x) / r ** 2, vy + w * (py - e.y) / r ** 2
        n = math.hypot(vx, vy) or 1.0
        grid = self.manager.zone_grid(self.feed.zone)
        for turn in (0.0, 0.6, -0.6, 1.2, -1.2, 2.0, -2.0):
            c, s = math.cos(turn), math.sin(turn)
            dx, dy = (vx * c - vy * s) / n, (vx * s + vy * c) / n
            tx, ty = px + dist * dx, py + dist * dy
            if grid is None or grid.free_at(tx, ty):
                return tx, ty
        return None

    def avoid_players(self, d: dict) -> bool:
        """Игроки рядом (в открытом мире) — по уровню угрозы: отойти или сбежать
        (зелье побега, маунт, дальше). True — пришлось уходить."""
        threats = self.threats(d)
        if not threats:
            return False
        top = threats[0]
        danger = top.level >= DANGER
        what = ", ".join(t.text() for t in threats[:3])
        self.status = ("опасно — убегает: " if danger else "уходит от игроков: ") + what
        self.note(self.status)
        self.alert(f"players:{top.name}:{int(self.clock() // 600)}",
                   "Бот: опасные игроки" if danger else "Бот: рядом игроки", what)
        if danger:
            if d.get("escape_key"):
                self.press_key(d["escape_key"])
                self.wait(0.5)
            if d.get("mount_key"):
                self.press_key(d["mount_key"])
                self.wait(2.5)
        target = self.away_point(threats, 60 if danger else 35)
        if target:
            self.walk_to(*target, tol=4, max_steps=16 if danger else 12)
        self.wait(self.rng.uniform(1, 3))
        return True

    def exits_around(self) -> list:
        return [e for e in self.feed.entities("object") if e.event in EXIT_EVENTS]

    def enter_exit(self, e, what: str = "выход") -> bool:
        """Дойти до портала или выхода и войти в него. True — зона сменилась."""
        old = self.feed.zone
        if not self.walk_to(e.x, e.y, tol=2.5, target=e.id):
            if self.feed.zone != old:
                return True
            self.note(f"{what}: не дошёл")
            return False
        for attempt in range(2):
            px, py = self.pos()
            self.click_world(e.x - px, e.y - py, step=0.4, keep_clear_of=False)
            if self.manager.config["points"].get("dungeon_enter"):
                self.wait(1.5)
                if self.feed.zone == old:
                    self.run_steps("вход в данж", "click @dungeon_enter", {})
            if self.wait_zone(old, 25 if attempt == 0 else 15):
                return True
        self.note(f"{what}: не удалось войти")
        return False

    def explore(self, visited: set, step: float = 22.0) -> None:
        """Идти туда, где ещё не были (по схеме зоны — только по проходимому), не наступая
        на выходы и порталы. Предпочтение — краю разведанного (а не случайным кругам)
        и направлениям, где рядом тоже не были."""
        px, py = self.pos()
        grid = self.manager.zone_grid(self.feed.zone)
        exits = [(e.x, e.y) for e in self.exits_around()]
        try:                                   # и обычные выходы зоны — не уйти в соседнюю
            exits += [(x, y) for x, y, _t in self.manager.router().exits(self.feed.zone)]
        except BotError:
            pass
        failed = getattr(self, "explore_failed", set())
        best, best_score = None, -1e9
        for i in range(12):
            ang = i * math.pi / 6 + self.rng.uniform(-0.2, 0.2)
            tx, ty = px + step * math.cos(ang), py + step * math.sin(ang)
            if grid is not None and not grid.free_at(tx, ty):
                continue
            if any(math.hypot(ex - tx, ey - ty) < 12 for ex, ey in exits):
                continue
            cell = (int(tx // EXPLORE_CELL), int(ty // EXPLORE_CELL))
            if cell in failed:
                continue
            around = sum((cell[0] + dx, cell[1] + dy) in visited for dx in (-1, 0, 1) for dy in (-1, 0, 1))
            score = (0 if cell in visited else 10) - 0.8 * around + self.rng.uniform(0, 2)
            if score > best_score:
                best, best_score = (tx, ty), score
        self.status = "разведка"
        if best is None:
            self.wander(20)
            return
        if not self.walk_to(*best, tol=3, max_steps=10):
            # Туда не пройти (стена, обрыв) — больше не пытаться в этой зоне.
            self.explore_failed = failed | {(int(best[0] // EXPLORE_CELL), int(best[1] // EXPLORE_CELL))}

    def mark_visited(self, visited: set) -> None:
        px, py = self.pos()
        cell = (int(px // EXPLORE_CELL), int(py // EXPLORE_CELL))
        if cell not in visited:
            visited.add(cell)
            if self.plan.get("zone") == self.feed.zone:
                self.plan["explored"] = [[c[0] * EXPLORE_CELL, c[1] * EXPLORE_CELL, EXPLORE_CELL]
                                         for c in list(visited)[-400:]]

    # --- бой и добыча ----------------------------------------------------
    def heal(self, d: dict) -> None:
        hp = self.feed.hp_pct
        if hp is None:
            return
        if d.get("potion_key") and hp < float(d.get("potion_hp") or 40) and self.clock() - self.potion_at > 30:
            self.press_key(d["potion_key"])
            self.potion_at = self.clock()
            self.note(f"здоровье {hp:.0f}% — зелье")
        if hp < float(d.get("retreat_hp") or 20):
            if self.floors and d.get("exit_key") and d.get("retreat_exit"):
                self.note(f"здоровье {hp:.0f}% — быстрый выход")
                raise DungeonAbort("hp")
            self.status = f"здоровье {hp:.0f}% — отход к входу"
            self.note(self.status)
            if self.home:
                self.walk_to(*self.home, tol=3)
            start = self.clock()
            while (self.feed.hp_pct or 100) < 90 and self.clock() - start < 90:
                self.wait_idle(2)

    def is_boss(self, mob) -> bool:
        radar = self.feed.radar
        info = radar.mobs.info(mob.type_id, radar.mob_offset) if radar.mobs is not None else None
        return bool(info and info.get("boss")) or "BOSS" in (mob.name or "").upper()

    def mob_info(self, mob) -> dict:
        radar = self.feed.radar
        return (radar.mobs.info(mob.type_id, radar.mob_offset) if radar.mobs is not None else None) or {}

    def mob_allowed(self, mob, d: dict) -> bool:
        info = self.mob_info(mob)
        max_tier = int(d.get("max_mob_tier") or 0)
        if max_tier and (info.get("tier") or 0) > max_tier:
            return False
        if d.get("skip_elite") and info.get("category") in ("champion", "elite") and not self.is_boss(mob):
            return False
        return True

    def pick_mob(self, d: dict, done: set, attacked: bool = False):
        """Следующий моб: ближний, но не из большой группы (если есть выбор). ``attacked`` —
        нас бьют: ближайший в 15 м без всяких фильтров."""
        px, py = self.pos()
        rng = float(d.get("attack_range") or 15)
        mobs = [e for e in self.feed.entities("mob") if e.id not in done and (e.health is None or e.health > 0)]
        if attacked:
            near = [m for m in mobs if math.hypot(m.x - px, m.y - py) <= 15]
            return min(near, key=lambda m: math.hypot(m.x - px, m.y - py)) if near else None
        mobs = [m for m in mobs if self.mob_allowed(m, d)]
        max_pack = int(d.get("max_pack") or 0)
        near = [m for m in mobs if math.hypot(m.x - px, m.y - py) <= rng]
        # С ограничением группы выбираем из более широкого круга: одиночка чуть дальше
        # лучше большой группы рядом.
        pool = [m for m in mobs if math.hypot(m.x - px, m.y - py) <= rng * 3] if max_pack or not near else near
        if not pool:
            return None

        def score(m):
            pack = sum(1 for o in mobs if math.hypot(o.x - m.x, o.y - m.y) < 8)
            over = max(0, pack - max_pack) if max_pack else 0
            return math.hypot(m.x - px, m.y - py) + 25 * over
        return min(pool, key=score)

    def fight(self, mob, d: dict, skills: list, done: set) -> None:
        boss = self.is_boss(mob)
        name = mob.name or f"моб {mob.id}"
        self.status = f"бой: {'босс ' if boss else ''}{name}"
        last_click = -1e9
        start = self.clock()
        first = True
        kite = bool(d.get("kite"))
        kite_dist = float(d.get("kite_dist") or 5)
        while True:
            self.check()
            self.check_alive()
            cur = self.feed.entity(mob.id)
            if cur is None or (cur.health is not None and cur.health <= 0):
                done.add(mob.id)
                self.kills += 1
                if boss:
                    self.boss_killed = True
                    self.note(f"босс побеждён: {name}")
                else:
                    self.note(f"побеждён {name} (всего {self.kills})")
                self.wait(self.rng.uniform(0.8, 1.6))      # добыча появляется не сразу
                return
            if self.clock() - start > FIGHT_TIMEOUT * (3 if boss else 1):
                done.add(mob.id)
                self.note(f"{name}: бой затянулся — пропускаю")
                return
            px, py = self.pos()
            dist = math.hypot(cur.x - px, cur.y - py)
            if kite and dist < kite_dist:
                # Дальний бой: отступить от моба и бить дальше.
                ax, ay = (px - cur.x) / max(dist, 0.1), (py - cur.y) / max(dist, 0.1)
                self.click_world(ax * 6, ay * 6, step=0.3)
                self.wait(0.6)
                last_click = -1e9
                continue
            if self.clock() - last_click > 2.5:
                # Клик по мобу — атака (и подход, если далеко).
                self.click_world(cur.x - px, cur.y - py, step=0.6, keep_clear_of=False)
                last_click = self.clock()
            hp = self.feed.hp_pct
            for s in skills:
                if self.clock() >= self.cooldowns.get(s.key, 0) and skill_ready(s, hp, boss, first):
                    self.press_key(s.key)
                    self.cooldowns[s.key] = self.clock() + s.cd
                    if s.cast:
                        self.wait(s.cast)         # каст: не двигаться
                    first = False
                    break
            self.heal(d)
            self.wait(0.6)

    def rest_between_packs(self, d: dict) -> None:
        """Мобов рядом нет, а здоровья мало — подождать восстановления перед следующей группой."""
        rest_hp = float(d.get("rest_hp") or 0)
        hp = self.feed.hp_pct
        if not rest_hp or hp is None or hp >= rest_hp:
            return
        px, py = self.pos()
        if any(math.hypot(m.x - px, m.y - py) < 12 for m in self.feed.entities("mob")):
            return
        self.status = f"восстанавливается ({hp:.0f}%)"
        self.note(self.status)
        start = self.clock()
        hits = self.feed.hits
        while (self.feed.hp_pct or 100) < 90 and self.clock() - start < 60 and self.feed.hits == hits:
            self.wait_idle(2)

    def pick_loot(self, d: dict, done: set):
        px, py = self.pos()
        out = []
        for e in self.feed.entities("loot"):
            if e.id in done or e.opened or e.event == "new_silver_object":    # серебро подбирается само
                continue
            chest = "chest" in e.event
            if (chest and not d.get("open_chests")) or (not chest and not d.get("loot_bags")):
                continue
            out.append(e)
        return min(out, key=lambda e: math.hypot(e.x - px, e.y - py)) if out else None

    def take_loot(self, e, d: dict, done: set) -> None:
        chest = "chest" in e.event
        what = "сундук" if chest else "добыча"
        self.status = "к сундуку" if chest else "к добыче"
        done.add(e.id)
        if not self.walk_to(e.x, e.y, tol=2.5, max_steps=25):
            self.note(f"{what}: не дошёл — пропускаю")
            return
        for attempt in range(2):
            px, py = self.pos()
            self.click_world(e.x - px, e.y - py, step=0.5, keep_clear_of=False)
            self.status = f"открывает: {what}"
            self.wait(float(d.get("chest_wait") or 8) if chest else 1.2)
            self.run_template("loot_all", {})
            self.wait(0.5)
            cur = self.feed.entity(e.id)
            if cur is None or cur.opened:
                break
            if attempt == 0:                # промах — подойти вплотную и ещё раз
                self.walk_to(e.x, e.y, tol=1.2, max_steps=6)
        else:
            self.note(f"{what}: не удалось забрать — пропускаю")
            return
        self.loots += 1
        if chest and self.boss_killed:
            self.boss_done = True
            self.note(f"финальный сундук открыт (добыча: {self.loots})")
        else:
            self.note(f"сундук открыт (добыча: {self.loots})" if chest else f"добыча собрана (всего: {self.loots})")

    # --- данж ---------------------------------------------------------------
    def start_dungeon_state(self) -> None:
        self.floors: list[tuple[str, tuple[float, float]]] = []
        self.floor_visited: dict[str, set] = {}
        self.explore_failed: set = set()
        self.last_hits = 0
        self.boss_killed = self.boss_done = False
        self.cooldowns: dict[str, float] = {}
        self.potion_at = -1e9

    def skills_of(self, d: dict) -> list:
        try:
            return parse_skills(d.get("skills"))
        except ValueError as e:
            raise BotError(f"данж: умения — {e}") from None

    def bag_full(self, d: dict) -> bool:
        slots = int(d.get("bag_slots") or 0)
        return self.feed.overloaded or bool(slots and self.feed.items_put - self.items_base >= slots)

    def clear_floor(self, d: dict, skills: list, started: float) -> str:
        """Зачистить этаж. «cleared» — пуст, «boss» — босс и его сундук, «players» — рядом
        игроки, «time» — время вышло, «bag» — сумка полна, «moved» — сменился этаж."""
        zone = self.feed.zone
        visited = self.floor_visited.setdefault(zone, set())
        done: set = set()
        last_found = self.clock()
        while True:
            self.check()
            self.check_alive()
            if self.feed.zone != zone:
                return "moved"
            if self.clock() - started > float(d.get("max_min") or 40) * 60:
                return "time"
            threats = self.threats(d)
            if threats:
                what = ", ".join(t.text() for t in threats[:3])
                self.alert(f"players:{threats[0].name}:{int(self.clock() // 600)}", "Бот: игроки в данже", what)
                self.note(f"игроки в данже: {what}")
                return "players"
            if self.bag_full(d):
                self.note("сумка полна — домой" if not self.feed.overloaded else "перегруз — домой")
                return "bag"
            self.mark_visited(visited)
            self.heal(d)
            if self.feed.hits != self.last_hits:
                # Нас бьют — сначала ответить ближайшему, даже если его бы пропустили.
                self.last_hits = self.feed.hits
                attacker = self.pick_mob(d, done, attacked=True)
                if attacker is not None:
                    self.note("атакован — отвечаю")
                    self.fight(attacker, d, skills, done)
                    self.last_hits = self.feed.hits
                    last_found = self.clock()
                    self.rest_between_packs(d)
                    continue
            mob = self.pick_mob(d, done)
            if mob is not None:
                self.fight(mob, d, skills, done)
                self.last_hits = self.feed.hits
                last_found = self.clock()
                self.rest_between_packs(d)
                continue
            loot = self.pick_loot(d, done)
            if loot is not None:
                self.take_loot(loot, d, done)
                last_found = self.clock()
                continue
            if self.boss_done:
                return "boss"          # босс, финальный сундук и вся добыча вокруг собраны
            if self.clock() - last_found > float(d.get("explore_min") or 4) * 60:
                return "cleared"
            self.explore(visited)

    def pick_exit(self, entry: tuple[float, float], used: set):
        px, py = self.pos()
        cands = [e for e in self.exits_around()
                 if e.id not in used and math.hypot(e.x - entry[0], e.y - entry[1]) > ENTRY_RADIUS]
        return min(cands, key=lambda e: math.hypot(e.x - px, e.y - py)) if cands else None

    def clear_dungeon(self, d: dict, skills: list) -> str:
        """Пройти данж этаж за этажом. Возвращает «done», «players» или «time»."""
        self.start_dungeon_state()
        self.last_hits = self.feed.hits
        started = self.clock()
        used: set = set()
        while True:
            zone, entry = self.feed.zone, self.pos()
            if not any(z == zone for z, _ in self.floors):
                self.floors.append((zone, entry))
                self.home = entry          # отходить при малом здоровье — сюда
                self.note(f"этаж {len(self.floors)}")
            try:
                result = self.clear_floor(d, skills, started)
            except DungeonAbort as e:
                return str(e)
            if result == "moved":
                self.explore_failed = set()
                continue
            if result in ("players", "time", "bag"):
                return result
            if result == "boss":
                return "done"
            if len(self.floors) >= int(d.get("max_floors") or 8):
                self.note("достигнут предел этажей")
                return "done"
            here = next(e for z, e in self.floors if z == self.feed.zone)
            ex = self.pick_exit(here, used)
            if ex is None:
                self.note("выхода на следующий этаж нет — данж пройден")
                return "done"
            used.add(ex.id)
            self.status = "на следующий этаж"
            if not self.enter_exit(ex, "выход на следующий этаж"):
                return "done"

    def leave_dungeon(self) -> None:
        """Выйти из данжа тем же путём: на каждом этаже — к точке появления и в выход рядом с ней."""
        for zone, entry in reversed(self.floors):
            self.check()
            if self.feed.zone != zone:
                continue
            self.status = "выход из данжа"
            self.walk_to(*entry, tol=3)
            back = [e for e in self.exits_around() if math.hypot(e.x - entry[0], e.y - entry[1]) <= ENTRY_RADIUS + 10]
            if not back:
                raise BotError("не найден выход назад у точки появления на этаже — выйдите из данжа вручную")
            ex = min(back, key=lambda e: math.hypot(e.x - entry[0], e.y - entry[1]))
            if not self.enter_exit(ex, "выход назад"):
                raise BotError("не удалось выйти из данжа — выйдите вручную")
        self.floors = []
        self.note(f"вышел из данжа в «{self.manager.zone_name(self.feed.zone)}»")

    def quick_exit(self, d: dict, skills: list | None = None) -> bool:
        """Быстрый выход из данжа клавишей (к входному порталу в открытом мире).

        Выход идёт с задержкой, урон его сбивает: перед нажатием бот добивает мобов
        рядом (если есть умения), во время задержки следит за своим здоровьем. True —
        вышли (сменилась зона)."""
        key = d.get("exit_key")
        if not key:
            return False
        channel = float(d.get("exit_channel") or 10)
        start_zone = self.feed.zone
        done: set = set()
        for attempt in range(3):
            self.check()
            if skills:
                while True:
                    px, py = self.pos()
                    near = [m for m in self.feed.entities("mob") if m.id not in done
                            and (m.health is None or m.health > 0) and math.hypot(m.x - px, m.y - py) < 12]
                    if not near:
                        break
                    self.fight(min(near, key=lambda m: math.hypot(m.x - px, m.y - py)), d, skills, done)
            hits0 = self.feed.hits
            self.status = "быстрый выход"
            self.press_key(key)
            t0 = self.clock()
            interrupted = False
            while self.clock() - t0 < channel + 8:
                self.wait(0.5)
                if self.feed.zone != start_zone:
                    self.wait(1.5)
                    self.floors = []
                    self.note(f"быстрый выход: «{self.manager.zone_name(self.feed.zone)}»")
                    return True
                if self.feed.hits != hits0:
                    interrupted = True
                    break
            self.note("быстрый выход сбит уроном — ещё раз" if interrupted else "быстрый выход не сработал")
        return False

    def exit_dungeon(self, d: dict, skills: list | None = None) -> None:
        """Выйти из данжа: быстрым выходом, а если не вышло — пешком по этажам."""
        if self.quick_exit(d, skills):
            return
        self.leave_dungeon()

    def dungeon(self) -> None:
        """Пройти данж, в котором стоит персонаж."""
        d = self.task_cfg("dungeon")
        skills = self.skills_of(d)
        result = self.clear_dungeon(d, skills)
        if result == "players":
            self.note("рядом игроки — выхожу из данжа")
            self.exit_dungeon(d)
        elif result in ("hp", "bag"):
            self.note("мало здоровья — выхожу" if result == "hp" else "сумка полна — выхожу")
            self.exit_dungeon(d, None if result == "hp" else skills)
        elif result == "time":
            self.note("время на данж вышло")
        else:
            self.note(f"данж пройден (убито: {self.kills}, добыча: {self.loots})")

    # --- цикл из города ----------------------------------------------------
    def pick_portal(self, d: dict):
        kinds = set(d.get("portal_kinds") or ["solo"])
        px, py = self.pos()
        radius = float(d.get("player_radius") or 45)
        out = []
        for e in self.feed.entities("object"):
            if e.event != "new_random_dungeon_exit" or e.id in self.bad_portals:
                continue
            if portal_kind(e.name) not in kinds:
                continue
            enchant = e.enchant or 0
            if not int(d.get("portal_enchant_min") or 0) <= enchant <= int(d.get("portal_enchant_max") or 4):
                continue
            if d.get("avoid_players") and [p for p in self.players_near(radius, e.x, e.y)
                                           if (p.name or "").lower() not in friends_of(d)]:
                continue
            out.append(e)
        return min(out, key=lambda e: math.hypot(e.x - px, e.y - py)) if out else None

    def find_portal(self, d: dict) -> None:
        """Из города — в ближайшие зоны, бегать по ним и войти в первый подходящий портал."""
        router = self.manager.router()
        safety = d.get("safety") or "yellow"
        start_zone = self.feed.zone
        zones = router.nearby(start_zone, safety, int(d.get("search_zones") or 4))
        if not zones:
            raise BotError(f"рядом с «{router.name(start_zone)}» нет зон с допустимой опасностью — "
                           "разрешите более опасные зоны")
        for _round in range(3):
            for zone in zones:
                self.check()
                portal = self.pick_portal(d)
                if portal is None and self.feed.zone != zone:
                    self.status = f"идёт искать данж: {router.name(zone)}"
                    self.travel_to(zone, safety=safety)
                    self.note(f"ищу данж в «{router.name(zone)}»")
                if self.search_zone(d):
                    return
                self.note(f"в «{router.name(self.feed.zone)}» подходящего данжа не нашёл")
        raise BotError("подходящих данжей в ближайших зонах нет — попробуйте больше зон или другие виды порталов")

    def search_zone(self, d: dict) -> bool:
        visited: set = set()
        deadline = self.clock() + float(d.get("search_min") or 8) * 60
        zone = self.feed.zone
        while self.clock() < deadline:
            self.check()
            self.check_alive()
            if self.feed.zone != zone:
                return False
            if self.avoid_players(d):
                continue
            if self.feed.hits != self.last_hits:
                self.last_hits = self.feed.hits
                px, py = self.pos()
                attacker = self.pick_mob(d, set(), attacked=True)
                if attacker is not None:
                    self.note("атакован мобом — отвечаю")
                    self.fight(attacker, d, self.skills_of(d), set())
                    continue
            portal = self.pick_portal(d)
            if portal is not None:
                self.status = f"к порталу ({portal_kind(portal.name)})"
                self.note(f"нашёл портал: {portal.name}")
                if self.enter_exit(portal, "портал"):
                    self.note("вошёл в данж")
                    return True
                self.bad_portals.add(portal.id)
                continue
            self.mark_visited(visited)
            self.explore(visited, step=40)
        return False

    def deposit(self, d: dict) -> None:
        self.status = "сдаёт добычу в сундук"
        if d.get("deposit_macro"):
            self.run_macro(d["deposit_macro"], {})
        else:
            self.run_template("stash_deposit", {})
        self.items_base = self.feed.items_put
        self.feed.overloaded = False
        self.note("добыча сдана в сундук")

    def services(self, d: dict) -> None:
        """Ремонт и докупка (зелья, еда) — каждые N данжей: дойти до места и выполнить макрос."""
        for kind, title in (("repair", "ремонт"), ("restock", "докупка")):
            every = int(d.get(f"{kind}_every") or 0)
            place, macro = d.get(f"{kind}_place"), d.get(f"{kind}_macro")
            if not every or not macro or self.runs == 0 or self.runs % every or self.serviced.get(kind) == self.runs:
                continue
            self.status = title
            if place:
                self.go_place(place, d.get("safety") or "yellow")
            self.run_macro(macro, {})
            self.serviced[kind] = self.runs
            self.note(f"{title}: готово")

    def dungeon_run(self) -> None:
        d = self.task_cfg("dungeon")
        skills = self.skills_of(d)
        home = d.get("home_place")
        if not home:
            raise BotError("данжи: выберите место сундука в городе (где сдавать добычу)")
        if home not in self.manager.config["places"]:
            raise BotError(f"нет сохранённого места «{home}»")
        self.bad_portals: set = set()
        self.items_base = self.feed.items_put
        self.serviced: dict = {}
        limit = int(d.get("runs") or 0)
        while True:
            self.check()
            home_zone = self.manager.config["places"][home]["zone"]
            if self.feed.zone != home_zone and not self.pick_portal(d):
                self.go_place(home, d.get("safety") or "yellow")
            self.find_portal(d)
            result = self.clear_dungeon(d, skills)
            notes = {"players": "в данже игроки — ухожу, ищу другой данж", "time": "время на данж вышло — выхожу",
                     "hp": "мало здоровья — выхожу и иду домой", "bag": "сумка полна — несу добычу домой"}
            if result in notes:
                self.note(notes[result])
            # При игроках рядом или малом здоровье — без боя: выход сразу.
            self.exit_dungeon(d, None if result in ("players", "hp") else skills)
            if result == "players":
                continue
            self.status = "домой"
            self.go_place(home, d.get("safety") or "yellow")
            self.deposit(d)
            if result == "done":
                self.runs += 1
                self.note(f"данж {self.runs} пройден: убито {self.kills}, добыча {self.loots}")
                self.alert(f"run:{self.runs}", "Бот: данж пройден",
                           f"данжей: {self.runs}, убито: {self.kills}, добыча: {self.loots}")
            self.services(d)
            if limit and self.runs >= limit:
                return
