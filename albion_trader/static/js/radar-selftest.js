"use strict";
// Тесты логики радара (без сервера): статусы игроков, фильтры, подписи, маршрут, отряды,
// стрелки, геометрия камеры и картинок, значки. Запускаются автотестами (Node, через
// tests/js/run_radar_js.js) и на странице radar-selftest.html в браузере — в том числе
// в Windows, чтобы проверить радар прямо на своём компьютере.

const RADAR_TESTS = [];
function rtest(name, fn) { RADAR_TESTS.push({ name, fn }); }
function eq(a, b, msg = "") {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) throw new Error(`${msg} ожидалось ${sb}, получено ${sa}`);
}
function near(a, b, eps = 1e-6, msg = "") {
  if (Math.abs(a - b) > eps) throw new Error(`${msg} ожидалось ≈${b}, получено ${a}`);
}
function ok(v, msg = "") { if (!v) throw new Error(`не выполнено: ${msg}`); }

const T_OPTS = (patch = {}) => {
  const o = { ...RADAR_DEFAULTS, ...patch };
  return { ...o, _friends: nameSet(o.friends), _ignore: nameSet(o.ignore) };
};
const ME = { x: 0, y: 0, zone_type: "OPENPVP_YELLOW" };
const P = (patch) => ({ kind: "player", id: 1, name: "Bob", guild: "", alliance: "", faction: 0, x: 0, y: 0, dist: 0, ...patch });

// --- статусы и видимость ---------------------------------------------------------
rtest("статус: свой по гильдии, альянсу и списку друзей (без учёта регистра)", () => {
  eq(playerStatus(P({ guild: "Wolves" }), ME, T_OPTS({ myguild: " wolves " })).key, "friend");
  eq(playerStatus(P({ alliance: "NORD" }), ME, T_OPTS({ myalliance: "nord" })).key, "friend");
  eq(playerStatus(P({ name: "Lira" }), ME, T_OPTS({ friends: "Tess,\n lira ; x" })).key, "friend");
  eq(playerStatus(P({ faction: 255, guild: "Wolves" }), ME, T_OPTS({ myguild: "Wolves" })).key, "friend");
});
rtest("статус: враждебный по флагу 255, в красной и чёрной зоне — все чужие", () => {
  eq(playerStatus(P({ faction: 255 }), ME, T_OPTS()).text, "враждебный (флаг)");
  eq(playerStatus(P(), { zone_type: "OPENPVP_RED" }, T_OPTS()).text, "красная зона");
  eq(playerStatus(P(), { zone_type: "OPENPVP_BLACK" }, T_OPTS()).text, "чёрная зона");
  eq(playerStatus(P({ faction: 2, flag: "Лимхерст" }), ME, T_OPTS()).key, "faction");
  eq(playerStatus(P({ faction: 2, flag: "Лимхерст" }), ME, T_OPTS()).color, FLAG_COLOR[2]);
  eq(playerStatus(P(), ME, T_OPTS()).key, "passive");
  eq(playerStatus(P(), {}, T_OPTS()).key, "passive");
});
rtest("видимость игроков: фильтры статусов и список скрываемых", () => {
  ok(!radarVisible(P({ name: "Spam" }), T_OPTS({ ignore: "spam" }), ME), "скрыт");
  ok(!radarVisible(P({ faction: 255 }), T_OPTS({ hostile: false }), ME), "враждебные выключены");
  ok(!radarVisible(P({ faction: 3 }), T_OPTS({ factional: false }), ME), "фракционные выключены");
  ok(!radarVisible(P(), T_OPTS({ passive: false }), ME), "мирные выключены");
  ok(radarVisible(P({ faction: 255 }), T_OPTS({ passive: false }), ME), "враждебный виден");
  ok(!radarVisible(P(), T_OPTS({ player: false }), ME), "слой игроков выключен");
});
rtest("ресурсы: матрица вид × тир, мин. тир, зачарование, цена", () => {
  const R = (p) => ({ kind: "resource", res: "ore", tier: 5, enchant: 0, value: 1000, ...p });
  ok(radarVisible(R(), T_OPTS(), ME), "по умолчанию виден");
  ok(!radarVisible(R(), T_OPTS({ resmatrix: { ore: { 5: false } } }), ME), "матрица");
  ok(radarVisible(R({ tier: 6 }), T_OPTS({ resmatrix: { ore: { 5: false } } }), ME), "другой тир");
  ok(!radarVisible(R({ tier: 3 }), T_OPTS({ mintier: 4 }), ME), "мин. тир");
  ok(!radarVisible(R(), T_OPTS({ minenchant: 1 }), ME), "мин. зачарование");
  ok(radarVisible(R({ enchant: 2 }), T_OPTS({ minenchant: 1 }), ME), "зачарование достаточно");
  ok(!radarVisible(R(), T_OPTS({ minvalue: 5000 }), ME), "мин. цена");
  ok(radarVisible(R({ value: null }), T_OPTS({ minvalue: 5000 }), ME), "без цены — не отсекаем");
  eq(resAllowed("ore", null, null, T_OPTS({ mintier: 4 })), true, "тир неизвестен");
});
rtest("мобы: мин. тир, только боссы, живые ресурсы под матрицу", () => {
  const M = (mob) => ({ kind: "mob", mob, enchant: 0 });
  ok(radarVisible(M(null), T_OPTS({ bossesonly: false }), ME), "без справочника");
  ok(!radarVisible(M(null), T_OPTS({ bossesonly: true }), ME), "без справочника — не босс");
  ok(!radarVisible(M({ tier: 3 }), T_OPTS({ mobmintier: 5 }), ME), "мин. тир");
  ok(radarVisible(M({ tier: 6, boss: true }), T_OPTS({ bossesonly: true }), ME), "босс");
  ok(!radarVisible(M({ tier: 6, res: "hide" }), T_OPTS({ resmatrix: { hide: { 6: false } } }), ME), "живая шкура в матрице");
  ok(radarVisible(M({ tier: 6, res: "hide" }), T_OPTS({ livingasres: false, resmatrix: { hide: { 6: false } } }), ME), "живые как мобы");
});

// --- подписи --------------------------------------------------------------------
rtest("подписи: игрок, ресурс, моб (обрезка длинных имён), сундук", () => {
  eq(radarLabel(P({ guild: "W", alliance: "A" })), "Bob");
  eq(radarLabel(P({ guild: "W", alliance: "A" }), true), "Bob [W] <A>");
  eq(radarLabel(P({ name: "VeryLongPlayerName123" })), "VeryLongPlayerN…");
  eq(radarLabel({ kind: "resource", name: "руда", tier: 6, enchant: 2, size: 4 }), "руда T6.2 ×4");
  eq(radarLabel({ kind: "resource", name: "руда", tier: null, enchant: 0, size: null }), "руда T?");
  const long = { kind: "mob", enchant: 1, mob: { tier: 7, name: "a".repeat(40), category_ru: "босс" } };
  eq(radarLabel(long), "T7.1");
  eq(radarLabel({ kind: "mob", enchant: 0, mob: { tier: 8, name: "x", category: "boss" } }), "Босс T8");
  eq(radarLabel({ kind: "mob", enchant: 0, mob: { tier: 6, name: "x", category: "elite" } }), "Элита T6");
  eq(radarLabel({ kind: "mob", enchant: 0, mob: { tier: 7, id: "T7_MOB_MISTS_SPIDER", name: "mists spider", category: "standard" } }), "T7");
  eq(radarLabel({ kind: "mob", enchant: 0, mob: { tier: 4, id: "MOB_MISTS_WISP_ENTRANCE", name: "wisp", category: "standard" } }), "Мгла");
  eq(radarLabel({ kind: "mob", enchant: 0, mob: { tier: 6, id: "MOB_UNIQUE_POWERCRYSTAL_TERRITORY", name: "c", category: "standard" } }), "Кристалл T6");
  eq(radarLabel({ kind: "loot", name: "TREASURE_MISTS_SOLO", event: "new_loot_chest" }), "Сундук");
  eq(radarLabel(long, true), `T7 ${"a".repeat(40)} .1 (босс)`);
  eq(radarLabel({ kind: "mob", type_id: 9, name: "", enchant: 0 }), "моб");
  eq(radarLabel({ kind: "loot", name: "сундук", event: "new_loot_chest", rarity: 3, opened: true }), "Сундук зол. ✓");
  eq(radarLabel({ kind: "loot", name: "сундук", event: "new_loot_chest", rarity: 3, opened: true }, true),
    "Сундук (легендарный) — открыт");
  eq(radarLabel({ kind: "object", name: "", event: "new_portal" }), "Портал");
  eq(radarLabel({ kind: "object", name: "RANDOMDUNGEON_SOLO_FOREST", event: "new_random_dungeon_exit", enchant: 2 }),
    "Данж соло .2");
  eq(radarLabel({ kind: "object", name: "RANDOMDUNGEON_GROUP_X", event: "new_random_dungeon_exit" }), "Данж");
  eq(radarLabel({ kind: "object", name: "PORTAL_MISTS_SOLO_ENTRANCE", event: "new_portal_entrance" }), "Мгла");
  eq(radarLabel({ kind: "object", name: "HELLGATE_2V2", event: "new_portal_entrance" }), "Адские врата");
  eq(radarLabel({ kind: "object", name: "WISP_ENTRANCE", event: "new_portal_entrance" }), "Мгла");
  eq(radarLabel({ kind: "object", name: "CORRUPTED_SOLO", event: "new_random_dungeon_exit" }), "Проклятый");
  eq(radarLabel({ kind: "object", name: "WHATEVER_THING", event: "new_whatever" }), "Объект");
  eq(radarLabel({ kind: "object", name: "WHATEVER_THING", event: "new_whatever" }, true), "Объект — WHATEVER_THING");
  eq(objectInfo({ name: "серебро", event: "new_silver_object" }).icon, "🪙");
  eq([fmtShort(950), fmtShort(25400), fmtShort(3200000), fmtShort(null)], ["950", "25k", "3.2M", "—"]);
  eq([...nameSet("A, b\n c;;")], ["a", "b", "c"]);
});
rtest("ссылка на значок предмета ресурса", () => {
  eq(resourceItemId({ res: "ore", tier: 5, enchant: 2 }), "T5_ORE_LEVEL2@2");
  eq(resourceItemId({ res: "wood", tier: 3, enchant: 0 }), "T3_WOOD");
  eq(resourceItemId({ res: "other", tier: 3 }), null);
});

// --- маршрут, отряды, стрелки -------------------------------------------------------
rtest("маршрут сбора: самые ценные узлы, обход «ближайший следующий»", () => {
  const R = (id, x, value) => ({ kind: "resource", id, x, y: 0, value });
  const ents = [R(1, 30, 100), R(2, 10, 50), R(3, 20, 900), R(4, -5, 1), P({ x: 1 })];
  // Длина 1 → в выборке 2 самых ценных (3 и 1), ближайший из них — 3.
  eq(gatheringRoute(ents, { x: 0, y: 0 }, 1).map((e) => e.id), [3]);
  // Длина 3 → в выборке все 4 узла, обход от ближайшего: 4 (5 м), 2, 3.
  eq(gatheringRoute(ents, { x: 0, y: 0 }, 3).map((e) => e.id), [4, 2, 3]);
  eq(gatheringRoute(ents, { x: 0, y: 0 }, 0), []);
  eq(gatheringRoute([], { x: 0, y: 0 }, 5), []);
  const noPrice = [{ kind: "resource", id: 5, x: 5, y: 0, tier: 8, enchant: 0 }, { kind: "resource", id: 6, x: 1, y: 0, tier: 2, enchant: 0 }];
  eq(gatheringRoute(noPrice, { x: 0, y: 0 }, 1).map((e) => e.id), [6]);        // оба в выборке (мин. 2)
});
rtest("отряды: 4+ враждебных цепочкой ближе 25 м, свои и мирные не считаются", () => {
  const H = (id, x, patch = {}) => P({ id, x, faction: 255, ...patch });
  const ents = [H(1, 0), H(2, 20), H(3, 40), H(4, 60), H(5, 200), H(6, 210), P({ id: 7, x: 70 }), H(8, 80, { guild: "Me" })];
  const squads = findSquads(ents, ME, T_OPTS({ myguild: "me" }));
  eq(squads.map((g) => g.map((p) => p.id).sort()), [[1, 2, 3, 4]]);
  eq(findSquads(ents, ME, T_OPTS(), 2).length, 2);
});
rtest("стрелки за краем: группировка по направлению и виду, ближайший в группе", () => {
  const E = (kind, dist) => ({ kind, dist });
  const groups = groupArrows([[E("player", 50), 100, 0], [E("player", 30), 100, 5], [E("loot", 40), 100, 2],
    [E("player", 70), -100, 0]], 0, 0);
  eq(groups.map((g) => [g.kind, g.n, g.e.dist]), [["player", 2, 30], ["loot", 1, 40], ["player", 1, 70]]);
});

// --- геометрия --------------------------------------------------------------------------
rtest("аффинные преобразования: произведение и обратное", () => {
  const m = [2, 1, -1, 3, 5, -7];
  const id = affMul(m, affInv(m));
  [1, 0, 0, 1, 0, 0].forEach((v, i) => near(id[i], v, 1e-9, `элемент ${i}`));
  const p = affMul([1, 0, 0, 1, 10, 20], [2, 0, 0, 2, 1, 1]);   // сначала масштаб, потом сдвиг
  eq(p, [2, 0, 0, 2, 11, 21]);
});
rtest("камера: за персонажем и статичная (вписана с поворотом и без)", () => {
  const v = Object.create(RadarView.prototype);
  v.opts = T_OPTS({ mode: "follow", zoom: 6 });
  v.data = { me: { x: 10, y: 20 } };
  eq(v.camera(500, 500), { x: 10, y: 20, scale: 6 });
  v.opts = T_OPTS({ mode: "static", rotate: false, staticzoom: 1 });
  v.map = { status: "ready", bounds: [-100, -100, 100, 100] };
  const c = v.camera(400, 400);
  eq([c.x, c.y], [0, 0]); near(c.scale, 0.9 * 400 / 200);
  v.opts = T_OPTS({ mode: "static", rotate: true, staticzoom: 2 });
  near(v.camera(400, 400).scale, 2 * 0.9 * 400 / (200 * Math.SQRT2));
  v.map = null; v.mapImg = null; v.anchor = null;                               // схемы нет — вокруг меня
  eq([v.camera(400, 400).x, v.camera(400, 400).y], [10, 20]);
  v.mapImg = { bounds: [0, 0, 50, 50] };
  eq(v.bounds(), [0, 0, 50, 50]);
});
rtest("поворот 45° как у камеры игры: север (+y) смотрит вверх-вправо", () => {
  const rot = -Math.PI / 4, cs = Math.cos(rot), sn = Math.sin(rot);
  const at = (x, y) => [(x * cs - y * sn), -(x * sn + y * cs)];
  const [nx, ny] = at(0, 1);
  ok(nx > 0 && ny < 0, "север вверх-вправо");
  const [tx, ty] = at(-1, 1);                                                   // угол (x0, y1) — верх ромба
  near(tx, 0); ok(ty < 0, "верхний угол");
});

// --- значки и звук (только там, где есть canvas) ---------------------------------------------
rtest("значки ресурсов: спрайты кэшируются по виду, тиру и зачарованию", () => {
  if (typeof document === "undefined" || !document.createElement("canvas").getContext) return "пропуск: нет canvas";
  const a = resourceSprite("ore", 6, 2), b = resourceSprite("ore", 6, 2), c = resourceSprite("ore", 6, 0);
  ok(a === b, "кэш"); ok(a !== c, "другое зачарование — другой спрайт");
  eq([a.width, a.height], [48, 48]);
  for (const r of ["wood", "rock", "fiber", "hide", "ore", "other"]) resourceSprite(r, 4, 1);
  return "";
});
rtest("отрисовка: полный кадр с объектами всех видов без ошибок", () => {
  if (typeof document === "undefined" || !document.createElement("canvas").getContext) return "пропуск: нет canvas";
  const box = document.createElement("div");
  box.style.cssText = "position:absolute;left:-2000px;width:400px;height:400px";
  const cv = document.createElement("canvas"); cv.style.cssText = "width:400px;height:400px";
  box.appendChild(cv); document.body.appendChild(box);
  try {
    const v = new RadarView(cv, {});
    v.setOpts(T_OPTS({ route: true, trails: true, alert: true, heat: true, squads: true, arrows: true, playerlabel: "power" }));
    v.map = { status: "ready", bounds: [-100, -100, 100, 100], height: [0, 10], exits_world: [[50, 50, "X", "ClusterExit", "Выход"]],
      tiles: [["ground", 0, 0, 20, 20, 0, 5], ["water", 10, 10, 10, 10, 45, 0], ["tree", 5, 5, 4, 4, 0, 0]] };
    v.image = renderZoneImage(v.map);
    v.heat = [{ x: 5, y: 5, res: "ore", tier: 5, enchant: 0, seen: 3 }];
    v.data = { me: { x: 0, y: 0, zone_type: "OPENPVP_BLACK" }, depleted: [{ x: 3, y: 3, res: "ore", tier: 5, enchant: 1, ago: 30, name: "руда" }],
      entities: [P({ id: 1, x: 5, y: 5, dist: 7, health: 10, max_health: 100, mounted: true, ip: 900, role: "хил" }),
        ...[2, 3, 4, 5].map((i) => P({ id: i, x: 400 + i, y: 0, dist: 400 })),
        { kind: "resource", id: 10, x: -5, y: 2, res: "ore", tier: 6, enchant: 2, size: 3, value: 900, dist: 5 },
        { kind: "mob", id: 11, x: 8, y: -3, enchant: 1, mob: { tier: 6, res: "hide", name: "wolf", boss: false }, dist: 8, age: 30 },
        { kind: "mob", id: 12, x: -8, y: -3, enchant: 0, mob: { tier: 8, name: "boss", boss: true }, dist: 8, age: 0 },
        { kind: "loot", id: 13, x: 2, y: -9, rarity: 3, name: "Сундук", dist: 9 },
        { kind: "object", id: 14, x: -2, y: 9, name: "портал", dist: 9 }] };
    v.trails.set(1, [[0, 0, 0], [Date.now() / 1000, 5, 5]]);
    v.flash = Date.now();
    v.draw();
    ok(v.hits.length >= 7, "объекты попали в список для подсказок");
    ok(v.tooltip(...v.hits.find((h) => h[2].id === 1).slice(0, 2)).includes("Bob"), "подсказка игрока");
    v.setOpts(T_OPTS({ mode: "static", resstyle: "text" }));
    v.draw();
  } finally { box.remove(); }
  return "";
});

// Запуск: возвращает [{name, ok, error, note}].
function runRadarSelfTest() {
  return RADAR_TESTS.map((t) => {
    try {
      const note = t.fn();
      return { name: t.name, ok: true, note: typeof note === "string" ? note : "" };
    } catch (e) {
      return { name: t.name, ok: false, error: String(e && e.message || e) };
    }
  });
}
if (typeof module !== "undefined") module.exports = { runRadarSelfTest };
