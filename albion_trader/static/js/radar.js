"use strict";
// Радар: игроки, мобы, ресурсы и лут вокруг персонажа (события New…, Move, Leave, …Object)
// на фоне схемы текущей зоны. Отрисовка (RadarView) общая для вкладки и окна radar.html;
// настройки задаются на вкладке (radar-tab.js) и хранятся в localStorage — окно
// подхватывает их сразу.
//
// Два вида: «за персонажем» (карта едет под вами, вы в центре) и «статичная карта»
// (вся зона неподвижна, двигаются точки — и вы тоже).

const RADAR_KINDS = [
  { kind: "player", title: "Игроки", color: "#e5484d" },
  { kind: "mob", title: "Мобы", color: "#f5a524" },
  { kind: "resource", title: "Ресурсы", color: "#30c47d" },
  { kind: "loot", title: "Лут и сундуки", color: "#4aa3ff" },
  { kind: "object", title: "Прочие объекты", color: "#b07cff" },
];
const RADAR_COLOR = Object.fromEntries(RADAR_KINDS.map((k) => [k.kind, k.color]));
const RADAR_KEY = "albion-trader-radar";
const RES_KINDS = [["wood", "дерево"], ["rock", "камень"], ["fiber", "волокно"], ["hide", "шкура"], ["ore", "руда"]];
const RADAR_DEFAULTS = {
  player: true, mob: true, resource: true, loot: true, object: true,
  mode: "follow", zoom: 4, staticzoom: 1, rotate: true, labels: true, background: true, bgopacity: 100, exits: true,
  resstyle: "icon", iconset: "drawn", mintier: 1, minenchant: 0, resmatrix: {}, reslabel: "tier", minvalue: 0,
  passive: true, factional: true, hostile: true, playerlabel: "name", moblabels: false, myguild: "", myalliance: "",
  friends: "", ignore: "",
  mobmintier: 1, bossesonly: false, livingasres: true,
  alert: false, alertradius: 60, alertsound: true, alertnotify: false,
  arrows: true, squads: true, trails: false, route: false, routelen: 8,
  depleted: true, respawnmin: 10, heat: false, stale: true,
};
const DEFAULT_PROFILES = {
  "Фарм": { player: true, mob: true, resource: true, loot: true, hostile: true, factional: false, passive: false,
    moblabels: true, bossesonly: false, route: false, heat: false, alert: true, trails: false, mintier: 4 },
  "PvP": { player: true, mob: false, resource: false, loot: true, hostile: true, factional: true, passive: true,
    playerlabel: "power", squads: true, trails: true, alert: true, arrows: true, route: false, heat: false },
  "Сбор": { player: true, mob: false, resource: true, loot: false, hostile: true, factional: false, passive: false,
    mintier: 5, route: true, depleted: true, heat: true, alert: true, resstyle: "icon", reslabel: "value" },
};

function radarOptions() { return { ...RADAR_DEFAULTS, ...loadSettings(RADAR_KEY) }; }
function saveRadarOptions(o) { saveSettings(o, RADAR_KEY); }

const nameSet = (text) => new Set(String(text || "").split(/[\n,;]+/).map((s) => s.trim().toLowerCase()).filter(Boolean));

// Цвета схемы зоны — как у карты зоны в игре: бирюзовая вода, охристая суша
// (светлее на возвышенностях), оранжевые дороги, светлые участки под дома.
const ZONE_STYLE = {
  base: "#7a6a30", outside: "#1b1d22", low: [118, 100, 42], high: [196, 168, 84],
  water: "#3f7f90", road: "#d4862f", cliff: "rgba(70,48,22,0.85)", building: "#9a948a",
  plot: "#d8c58c", plotEdge: "rgba(110,85,35,0.75)", path: "#5b3d1d",
  rock: "#8b8174", tree: "#566428", frame: "#d9b98a", frameEdge: "#7a5a33",
};
const ZONE_ORDER = ["ground", "water", "plot", "road", "path", "cliff", "building", "rock", "tree"];

// Цвета тиров и зачарований — как в игре.
const TIER_COLOR = { 1: "#a3a3a3", 2: "#c8c8c8", 3: "#5bbf5b", 4: "#4a90e2", 5: "#e5484d", 6: "#f28c28",
  7: "#f5d142", 8: "#f4f4f4" };
const ENCHANT_COLOR = { 1: "#3ddc84", 2: "#4aa3ff", 3: "#b07cff", 4: "#f5c542" };
const RES_ITEM = { wood: "WOOD", rock: "ROCK", fiber: "FIBER", hide: "HIDE", ore: "ORE" };
const RES_COLOR = { wood: "#8bc34a", rock: "#b0b0b0", fiber: "#f0e68c", hide: "#c98a4b", ore: "#5fa8ff" };
// Редкость сундука: обычный, необычный, редкий, легендарный.
const CHEST_COLOR = { 0: "#d9d9d9", 1: "#3ddc84", 2: "#4aa3ff", 3: "#f5c542" };
const CHEST_RARITY = { 0: "обычный", 1: "необычный", 2: "редкий", 3: "легендарный" };
// Флаги фракций (NewCharacter[53]) — цвета городов.
const FLAG_COLOR = { 1: "#3c7dd9", 2: "#4caf50", 3: "#f08c2e", 4: "#e8e8e8", 5: "#8e5bd6", 6: "#c62828" };
const STATUS_STYLE = {
  friend: { text: "свой", color: "#4aa3ff" },
  hostile: { text: "враждебный", color: "#ff3b3b" },
  faction: { text: "фракция", color: "#f5a524" },
  passive: { text: "мирный", color: "#9be29b" },
};
const STALE_SECONDS = 20;     // игрок или моб без событий дольше — показываем бледным

// Статус игрока: свой (гильдия/альянс/список друзей), враждебный (флаг 255, или красная/чёрная
// зона — там напасть может любой), фракционный (флаг города 1–6) или мирный.
function playerStatus(e, me, o) {
  const same = (a, b) => a && b && a.trim().toLowerCase() === b.trim().toLowerCase();
  if (same(e.guild, o.myguild) || same(e.alliance, o.myalliance) || (o._friends || nameSet(o.friends)).has((e.name || "").toLowerCase())) {
    return { key: "friend", ...STATUS_STYLE.friend };
  }
  if (e.faction === 255) return { key: "hostile", text: "враждебный (флаг)", color: STATUS_STYLE.hostile.color };
  const zt = (me.zone_type || "").toUpperCase();
  if (zt.includes("BLACK")) return { key: "hostile", text: "чёрная зона", color: STATUS_STYLE.hostile.color };
  if (zt.includes("RED")) return { key: "hostile", text: "красная зона", color: STATUS_STYLE.hostile.color };
  if (e.faction >= 1 && e.faction <= 6) {
    return { key: "faction", text: `фракция: ${e.flag}`, color: FLAG_COLOR[e.faction] };
  }
  return { key: "passive", ...STATUS_STYLE.passive };
}

// Подписи на карте — короткие (до SHORT_LEN символов), полные — в подсказке.
const SHORT_LEN = 16;
const short = (t, n = SHORT_LEN) => (t && t.length > n ? t.slice(0, n - 1) + "…" : t || "");

// Объекты по имени из события (RANDOMDUNGEON_SOLO_…, …MISTS…) и виду события: короткое
// русское название, значок и цвет. Порядок важен — первое совпадение.
const OBJECT_TYPES = [
  [/MIST|WISP/, "Мгла", "🌫", "#7fd3ff"],
  [/HELLGATE/, "Адские врата", "🔥", "#ff5a36"],
  [/CORRUPT/, "Проклятый", "☠", "#e5484d"],
  [/AVALON|ROADS/, "Авалон", "🌀", "#f5c542"],
  [/RANDOMDUNGEON.*SOLO|SOLO.*DUNGEON/, "Данж соло", "🟢", "#3ddc84"],
  [/RANDOMDUNGEON|DUNGEON|NEW_RANDOM_DUNGEON_EXIT/, "Данж", "🔵", "#4aa3ff"],
  [/EXPEDITION/, "Экспедиция", "🧭", "#c9a0ff"],
  [/ARENA/, "Арена", "⚔", "#ff8f6b"],
  [/SHRINE/, "Святилище", "✨", "#ffe678"],
  [/FISH/, "Рыба", "🐟", "#5fc7ff"],
  [/SILVER/, "Серебро", "🪙", "#d9d9d9"],
  [/TREASURE/, "Клад", "💎", "#7fe3ff"],
  [/CHEST/, "Сундук", "📦", "#c98a4b"],
  [/LOOT|CORPSE/, "Добыча", "💰", "#4aa3ff"],
  [/ENTRANCE|PORTAL/, "Портал", "🌀", "#b07cff"],
  [/EXIT/, "Выход", "🚪", "#f2d27a"],
];
const CHEST_SHORT = { 0: "", 1: "зел.", 2: "син.", 3: "зол." };

// Сундуки и добыча — по виду события (в имени сундука бывает MISTS, HELLGATE…).
const LOOT_TYPES = [
  [/TREASURE_CHEST/, "Клад", "💎", "#7fe3ff"],
  [/CHEST/, "Сундук", "📦", "#c98a4b"],
  [/LOOT/, "Добыча", "💰", "#4aa3ff"],
];

function objectInfo(e) {
  const ev = (e.event || "").toUpperCase();
  for (const [re, name, icon, color] of LOOT_TYPES) {
    if (re.test(ev)) return { name, icon, color };
  }
  const key = `${e.name || ""} ${ev}`.toUpperCase();
  for (const [re, name, icon, color] of OBJECT_TYPES) {
    if (re.test(key)) return { name, icon, color };
  }
  return { name: "Объект", icon: "❔", color: RADAR_COLOR.object };
}

// Вид моба для карты: значок, короткое слово, цвет. Мобы различаются видом и тиром,
// длинное имя — только в подсказке.
const MOB_KINDS = {
  wisp: { word: "Мгла", icon: "🌫", color: "#7fd3ff" },
  boss: { word: "Босс", icon: "👑", color: "#ff7a1a" },
  champion: { word: "Чемп.", icon: "👑", color: "#ff9f43" },
  miniboss: { word: "Мини-босс", icon: "👑", color: "#ffb86b" },
  elite: { word: "Элита", icon: "⭐", color: "#ffd166" },
  chest: { word: "Сундук", icon: "📦", color: "#c98a4b" },
  crystal: { word: "Кристалл", icon: "💠", color: "#9b8cff" },
  harmless: { word: "", icon: "", color: "#9be29b" },
  mob: { word: "", icon: "", color: RADAR_COLOR.mob },
};

function mobKind(e) {
  const m = e.mob;
  const id = `${(m && m.id) || ""} ${e.name || ""}`.toUpperCase();
  if (/WISP/.test(id)) return "wisp";                 // шар — вход в Мглу
  if (/POWERCRYSTAL|CRYSTAL_/.test(id)) return "crystal";
  const cat = m && m.category;
  return MOB_KINDS[cat] && cat !== "mob" ? cat : "mob";
}

function mobTitle(e, full = false) {
  const m = e.mob;
  const tier = m ? `T${m.tier ?? "?"}` : "";
  const ench = e.enchant ? `.${e.enchant}` : "";
  if (full) {
    const base = m ? `${tier} ${m.name}` : (e.name || `моб #${e.type_id ?? "?"}`);
    return base + (e.enchant ? ` .${e.enchant}` : "") + (m && m.category_ru ? ` (${m.category_ru})` : "");
  }
  const kind = mobKind(e), word = MOB_KINDS[kind].word;
  if (kind === "wisp" || kind === "chest") return word;   // тир входа в Мглу и сундука не нужен
  if (!m) return word || `моб${e.type_id != null ? " #" + e.type_id : ""}`;
  return [word, tier + ench].filter(Boolean).join(" ");
}

// Короткая подпись (на карте и в списке); full — полная (подсказка).
function radarLabel(e, full = false) {
  if (e.kind === "player") {
    return full ? e.name + (e.guild ? ` [${e.guild}]` : "") + (e.alliance ? ` <${e.alliance}>` : "") : short(e.name);
  }
  if (e.kind === "resource") {
    return `${e.name} T${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}${e.size != null ? " ×" + e.size : ""}`;
  }
  if (e.kind === "mob") return mobTitle(e, full);
  const info = objectInfo(e);
  const ench = e.enchant ? ` .${e.enchant}` : "";
  if (e.kind === "loot" && e.rarity != null) {
    const r = full ? CHEST_RARITY[e.rarity] : CHEST_SHORT[e.rarity];
    return `${info.name}${r ? ` ${full ? `(${r})` : r}` : ""}${e.opened ? (full ? " — открыт" : " ✓") : ""}`;
  }
  return info.name + ench + (full && e.name && e.name !== info.name ? ` — ${e.name}` : "");
}

// Значок объекта: цветной кружок и символ внутри.
function drawObjectIcon(ctx, e, sx, sy, dense, dim = false, kind = null) {
  const info = kind ? { name: kind.word, icon: kind.icon, color: kind.color } : objectInfo(e);
  const r = dense ? 6 : 8;
  ctx.globalAlpha *= dim ? 0.5 : 1;
  ctx.fillStyle = "rgba(10,12,16,0.75)"; ctx.strokeStyle = info.color; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(sx, sy, r, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
  ctx.font = `${dense ? 9 : 11}px "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", system-ui, sans-serif`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillStyle = "#fff"; ctx.fillText(info.icon, sx, sy + 0.5);
  ctx.textBaseline = "alphabetic";
  ctx.font = "11px system-ui, sans-serif";
  if (dim) ctx.globalAlpha /= 0.5;
  return info;
}

// Матрица фильтра ресурсов: вид × тир (пусто — всё разрешено) + мин. тир и зачарование.
function resAllowed(res, tier, enchant, o) {
  if (tier != null && tier < o.mintier) return false;
  if ((enchant || 0) < (o.minenchant || 0)) return false;
  const m = o.resmatrix || {};
  return !(m[res] && m[res][tier] === false);
}

function radarVisible(e, o, me) {
  if (!o[e.kind]) return false;
  if (e.kind === "resource") {
    if (!resAllowed(e.res, e.tier, e.enchant, o)) return false;
    return !(o.minvalue && e.value != null && e.value < o.minvalue);
  }
  if (e.kind === "player") {
    if ((o._ignore || nameSet(o.ignore)).has((e.name || "").toLowerCase())) return false;
    const k = playerStatus(e, me || {}, o).key;
    return k === "hostile" ? o.hostile : k === "faction" ? o.factional : o.passive;
  }
  if (e.kind === "mob") {
    const m = e.mob;
    if (m && m.tier && m.tier < o.mobmintier) return false;
    if (o.bossesonly && !(m && m.boss)) return false;
    if (m && m.res && o.livingasres && !resAllowed(m.res, m.tier, e.enchant, o)) return false;
  }
  return true;
}

// Аффинное преобразование [a, b, c, d, e, f] (как у canvas): x' = a·x + c·y + e, y' = b·x + d·y + f.
function affMul(m, n) {   // сначала n, потом m
  return [m[0] * n[0] + m[2] * n[1], m[1] * n[0] + m[3] * n[1], m[0] * n[2] + m[2] * n[3], m[1] * n[2] + m[3] * n[3],
    m[0] * n[4] + m[2] * n[5] + m[4], m[1] * n[4] + m[3] * n[5] + m[5]];
}

// Короткий звуковой сигнал (без файлов).
let AUDIO = null;
function beep(freq = 880, ms = 160) {
  try {
    AUDIO = AUDIO || new (window.AudioContext || window.webkitAudioContext)();
    const o = AUDIO.createOscillator(), g = AUDIO.createGain();
    o.frequency.value = freq; o.type = "square"; g.gain.value = 0.05;
    o.connect(g); g.connect(AUDIO.destination); o.start(); o.stop(AUDIO.currentTime + ms / 1000);
  } catch { /* звук недоступен */ }
}

// --- значки ---------------------------------------------------------------
// Значки предметов с официального сервера картинок игры (render.albiononline.com) —
// только если выбран вариант «из игры»; пока картинка грузится, рисуется свой значок.
const GAME_ICONS = new Map();
function gameIcon(itemId) {
  let im = GAME_ICONS.get(itemId);
  if (!im) {
    im = new Image();
    im.src = `https://render.albiononline.com/v1/item/${encodeURIComponent(itemId)}.png?size=64`;
    GAME_ICONS.set(itemId, im);
  }
  return im.complete && im.naturalWidth ? im : null;
}

function resourceItemId(e) {
  const r = RES_ITEM[e.res];
  if (!r || !e.tier) return null;
  return `T${e.tier}_${r}` + (e.enchant ? `_LEVEL${e.enchant}@${e.enchant}` : "");
}

// Свои значки ресурсов — «узлы» в духе карты игры: валун с кристаллами (руда), груда
// глыб (камень), дерево, куст волокна, шкура. Цвет кристаллов и камешка-тира внизу —
// тир, ореол — зачарование. Каждый вариант рисуется один раз в спрайт 48×48 и кэшируется.
const RES_SPRITES = new Map();

function shade(hex, f) {   // f > 0 — светлее, f < 0 — темнее
  const n = parseInt(hex.slice(1), 16);
  const c = [n >> 16, (n >> 8) & 255, n & 255].map((v) => Math.round(f > 0 ? v + (255 - v) * f : v * (1 + f)));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function poly(ctx, pts) {
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  for (const [x, y] of pts.slice(1)) ctx.lineTo(x, y);
  ctx.closePath();
}

function boulder(ctx, pts, light, dark) {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const g = ctx.createLinearGradient(Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys));
  g.addColorStop(0, light); g.addColorStop(1, dark);
  poly(ctx, pts); ctx.fillStyle = g; ctx.fill();
  ctx.strokeStyle = "rgba(20,16,14,0.9)"; ctx.lineWidth = 1.5; ctx.stroke();
}

function crystal(ctx, x, y, w, h, angle, color) {
  ctx.save(); ctx.translate(x, y); ctx.rotate(angle);
  const g = ctx.createLinearGradient(-w, 0, w, 0);
  g.addColorStop(0, shade(color, 0.55)); g.addColorStop(0.5, color); g.addColorStop(1, shade(color, -0.45));
  poly(ctx, [[0, -h], [w, -h * 0.55], [w * 0.8, 0], [-w * 0.8, 0], [-w, -h * 0.55]]);
  ctx.fillStyle = g; ctx.fill();
  ctx.strokeStyle = "rgba(10,10,20,0.85)"; ctx.lineWidth = 1.2; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(-w * 0.35, -h * 0.75); ctx.lineTo(-w * 0.45, -h * 0.15);
  ctx.strokeStyle = "rgba(255,255,255,0.75)"; ctx.lineWidth = 1.2; ctx.stroke();
  ctx.restore();
}

function resourceSprite(res, tier, enchant) {
  const key = `${res}:${tier}:${enchant}`;
  let cv = RES_SPRITES.get(key);
  if (cv) return cv;
  cv = document.createElement("canvas"); cv.width = cv.height = 48;
  const ctx = cv.getContext("2d"), tc = TIER_COLOR[tier] || "#9aa0a6";
  ctx.lineJoin = "round";
  if (enchant) {   // ореол зачарования
    const g = ctx.createRadialGradient(24, 26, 6, 24, 26, 23);
    g.addColorStop(0, ENCHANT_COLOR[enchant] + "cc"); g.addColorStop(1, ENCHANT_COLOR[enchant] + "00");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(24, 26, 23, 0, 2 * Math.PI); ctx.fill();
  }
  ctx.fillStyle = "rgba(0,0,0,0.35)";
  ctx.beginPath(); ctx.ellipse(24, 39, 15, 4.5, 0, 0, 2 * Math.PI); ctx.fill();
  switch (res) {
    case "ore":    // тёмный валун с кристаллами цвета тира
      boulder(ctx, [[9, 38], [7, 29], [13, 21], [22, 19], [32, 21], [40, 28], [39, 38]], "#7d746c", "#2f2a27");
      crystal(ctx, 17, 27, 4.5, 14, -0.45, tc);
      crystal(ctx, 25, 26, 5.5, 18, 0.05, tc);
      crystal(ctx, 33, 29, 4, 11, 0.5, tc);
      break;
    case "rock": { // груда глыб, слегка подкрашенных тиром
      const light = shade(tc, 0.25), base = "#8d8a86";
      boulder(ctx, [[8, 38], [7, 30], [13, 25], [20, 27], [22, 38]], "#a9a6a2", "#4a4744");
      boulder(ctx, [[20, 38], [19, 27], [27, 22], [36, 24], [41, 31], [39, 38]], base, "#3d3a38");
      boulder(ctx, [[14, 27], [16, 17], [24, 12], [31, 16], [30, 25], [21, 28]], light, shade(tc, -0.55));
      break;
    }
    case "wood":   // дерево: ствол и крона, тир — цвет листвы по краю
      ctx.fillStyle = "#5a3b22"; ctx.strokeStyle = "#24160c"; ctx.lineWidth = 1.5;
      poly(ctx, [[21, 39], [22, 26], [26, 26], [28, 39]]); ctx.fill(); ctx.stroke();
      for (const [x, y, r] of [[17, 22, 9], [31, 22, 9], [24, 15, 10]]) {
        const g = ctx.createRadialGradient(x - 3, y - 3, 2, x, y, r);
        g.addColorStop(0, "#5f9a4c"); g.addColorStop(1, "#24502a");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.fill();
        ctx.strokeStyle = "rgba(10,25,10,0.9)"; ctx.stroke();
      }
      break;
    case "fiber":  // куст с пушистыми коробочками
      ctx.strokeStyle = "#2c5a24"; ctx.lineWidth = 3; ctx.lineCap = "round";
      for (const [x, y] of [[13, 16], [19, 11], [26, 10], [33, 13], [37, 20]]) {
        ctx.beginPath(); ctx.moveTo(25, 38); ctx.quadraticCurveTo(25, 26, x, y); ctx.stroke();
      }
      for (const [x, y] of [[13, 16], [19, 11], [26, 10], [33, 13], [37, 20]]) {
        ctx.fillStyle = "#f2efe2"; ctx.strokeStyle = "#5d5a4c"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
      }
      break;
    case "hide": { // растянутая шкура
      const g = ctx.createLinearGradient(10, 10, 38, 40);
      g.addColorStop(0, "#b07a4a"); g.addColorStop(1, "#5a3519");
      ctx.beginPath();
      ctx.moveTo(18, 9); ctx.quadraticCurveTo(24, 14, 30, 9); ctx.lineTo(38, 16); ctx.quadraticCurveTo(33, 24, 39, 32);
      ctx.lineTo(31, 39); ctx.quadraticCurveTo(24, 34, 17, 39); ctx.lineTo(9, 32); ctx.quadraticCurveTo(15, 24, 10, 16);
      ctx.closePath(); ctx.fillStyle = g; ctx.fill();
      ctx.strokeStyle = "#2b1a0c"; ctx.lineWidth = 1.5; ctx.stroke();
      break;
    }
    default:
      boulder(ctx, [[12, 36], [12, 18], [36, 18], [36, 36]], "#7fd09a", "#2f6b44");
  }
  // Камешек тира (у руды тир и так виден по кристаллам).
  if (res !== "ore" && TIER_COLOR[tier]) {
    poly(ctx, [[40, 33], [45, 38], [40, 44], [35, 38]]);
    ctx.fillStyle = tc; ctx.fill(); ctx.strokeStyle = "#111"; ctx.lineWidth = 1.3; ctx.stroke();
  }
  RES_SPRITES.set(key, cv);
  return cv;
}

function drawResourceIcon(ctx, e, x, y, size = 24) {
  ctx.drawImage(resourceSprite(e.res, e.tier, e.enchant || 0), x - size / 2, y - size * 0.6, size, size);
}

function drawText(ctx, text, x, y, color = "#f1f1f1") {
  ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.fillStyle = color;
  ctx.strokeText(text, x, y); ctx.fillText(text, x, y);
}

// Схема зоны → картинка (offscreen canvas) в координатах зоны; рисуется один раз на зону.
function renderZoneImage(map) {
  const [x0, z0, x1, z1] = map.bounds;
  const w = x1 - x0, h = z1 - z0;
  const k = Math.min(3, 2400 / Math.max(w, h, 1));
  const cv = document.createElement("canvas");
  cv.width = Math.max(1, Math.round(w * k)); cv.height = Math.max(1, Math.round(h * k));
  const ctx = cv.getContext("2d");
  ctx.fillStyle = ZONE_STYLE.base; ctx.fillRect(0, 0, cv.width, cv.height);
  const [lo, hi] = map.height && map.height[1] > map.height[0] ? map.height : [0, 16];
  const byCat = {};
  for (const t of map.tiles) (byCat[t[0]] = byCat[t[0]] || []).push(t);
  for (const cat of ZONE_ORDER) {
    for (const [, x, z, tw, th, rot, y] of byCat[cat] || []) {
      if (cat === "ground") {
        const f = Math.max(0, Math.min(1, (y - lo) / (hi - lo)));
        const c = ZONE_STYLE.low.map((v, i) => Math.round(v + (ZONE_STYLE.high[i] - v) * f));
        ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      } else ctx.fillStyle = ZONE_STYLE[cat];
      ctx.save();
      ctx.translate((x - x0) * k, (z1 - z) * k);
      ctx.rotate(rot * Math.PI / 180);
      if (cat === "tree" || cat === "rock") {
        ctx.beginPath(); ctx.arc(0, 0, Math.max(tw, th) * k / 2, 0, 2 * Math.PI); ctx.fill();
      } else {
        ctx.fillRect(-tw * k / 2, -th * k / 2, tw * k, th * k);
        if (cat === "plot") {
          ctx.strokeStyle = ZONE_STYLE.plotEdge; ctx.lineWidth = Math.max(1, k * 0.6);
          ctx.strokeRect(-tw * k / 2, -th * k / 2, tw * k, th * k);
        }
      }
      ctx.restore();
    }
  }
  return { canvas: cv, k, x0, z1 };
}

function affInv(m) {
  const det = m[0] * m[3] - m[1] * m[2];
  const a = m[3] / det, b = -m[1] / det, c = -m[2] / det, d = m[0] / det;
  return [a, b, c, d, -(a * m[4] + c * m[5]), -(b * m[4] + d * m[5])];
}

class RadarView {
  constructor(canvas, { hud = null, compact = false } = {}) {
    this.canvas = canvas;
    this.hud = hud;
    this.compact = compact;
    this.setOpts(radarOptions());
    this.data = { me: { x: 0, y: 0 }, entities: [], codes: [], depleted: [] };
    this.zone = null;        // id зоны, для которой загружена/грузится схема
    this.map = null;         // {status, ...схема} с сервера
    this.image = null;       // отрисованная схема
    this.mapImg = null;      // своя картинка карты зоны (data/maps)
    this.anchor = null;      // центр статичной карты, если схемы зоны нет
    this.heat = [];          // тепловая карта ресурсов зоны
    this.trails = new Map(); // id → [[t, x, y], …]
    this.alerted = new Map();
    this.flash = 0;
    window.addEventListener("storage", (ev) => { if (ev.key === RADAR_KEY) this.setOpts(radarOptions()); });
    this.tip = document.createElement("div");
    this.tip.className = "radar-tip"; this.tip.hidden = true;
    canvas.parentElement.appendChild(this.tip);
    canvas.addEventListener("mousemove", (ev) => {
      const html = this.tooltip(ev.offsetX, ev.offsetY);
      this.tip.hidden = !html;
      if (!html) return;
      this.tip.innerHTML = html;
      const pw = canvas.clientWidth, left = ev.offsetX + 14;
      this.tip.style.left = `${Math.min(left, pw - this.tip.offsetWidth - 4)}px`;
      this.tip.style.top = `${ev.offsetY + 14}px`;
    });
    canvas.addEventListener("mouseleave", () => { this.tip.hidden = true; });
  }

  setOpts(o) {
    this.opts = { ...o, _friends: nameSet(o.friends), _ignore: nameSet(o.ignore) };
  }

  // Изменить настройку (горячие клавиши, профили) — сохраняется для вкладки и окна.
  update(patch) {
    const o = { ...radarOptions(), ...patch };
    saveRadarOptions(o);
    this.setOpts(o);
    if (this.onchange) this.onchange(o);
  }

  // Горячие клавиши: +/− масштаб, M вид, L подписи, B фон, R поворот, H тепловая карта,
  // T следы, 1–5 слои, P следующий профиль.
  bindKeys(target = window) {
    target.addEventListener("keydown", (ev) => {
      if (ev.target && /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
      if (target === window && this.keysOnlyWhen && !this.keysOnlyWhen()) return;
      const o = this.opts, k = ev.key.toLowerCase();
      const flip = (key) => this.update({ [key]: !o[key] });
      if (k === "+" || k === "=") {
        if (o.mode === "static") this.update({ staticzoom: Math.min(4, (o.staticzoom || 1) + 0.25) });
        else this.update({ zoom: Math.min(20, o.zoom + 0.5) });
      } else if (k === "-" || k === "_") {
        if (o.mode === "static") this.update({ staticzoom: Math.max(1, (o.staticzoom || 1) - 0.25) });
        else this.update({ zoom: Math.max(1, o.zoom - 0.5) });
      } else if (k === "m" || k === "ь") this.update({ mode: o.mode === "static" ? "follow" : "static" });
      else if (k === "l" || k === "д") flip("labels");
      else if (k === "b" || k === "и") flip("background");
      else if (k === "r" || k === "к") flip("rotate");
      else if (k === "h" || k === "р") flip("heat");
      else if (k === "t" || k === "е") flip("trails");
      else if (k === "p" || k === "з") this.nextProfile();
      else if (/^[1-5]$/.test(k)) flip(RADAR_KINDS[Number(k) - 1].kind);
      else return;
      ev.preventDefault();
    });
  }

  nextProfile() {
    const all = { ...DEFAULT_PROFILES, ...loadSettings("albion-trader-radar-profiles") };
    const names = Object.keys(all);
    const cur = names.indexOf(this.opts.profile);
    const name = names[(cur + 1) % names.length];
    this.update({ ...all[name], profile: name });
  }

  async poll() {
    try {
      this.data = await api("/api/radar");
      this.error = null;
      this.syncZone();
      this.trackTrails();
      this.checkAlerts();
    } catch (e) { this.error = e.message; }
    return this.data;
  }

  trackTrails() {
    const now = Date.now() / 1000, keep = 8;
    const alive = new Set();
    for (const e of this.data.entities) {
      if (e.kind !== "player" && e.kind !== "mob") continue;
      alive.add(e.id);
      const t = this.trails.get(e.id) || [];
      const last = t[t.length - 1];
      if (!last || last[1] !== e.x || last[2] !== e.y) t.push([now, e.x, e.y]);
      while (t.length && now - t[0][0] > keep) t.shift();
      this.trails.set(e.id, t);
    }
    for (const id of this.trails.keys()) if (!alive.has(id)) this.trails.delete(id);
  }

  // Оповещение: враждебный игрок ближе заданного — звук, вспышка рамки и (по желанию)
  // оповещение программы (Windows, Telegram, Discord). Не чаще раза в 5 минут на игрока.
  checkAlerts() {
    const o = this.opts;
    if (!o.alert) return;
    const now = Date.now(), me = this.data.me;
    for (const e of this.data.entities) {
      if (e.kind !== "player" || e.dist > o.alertradius) continue;
      if (playerStatus(e, me, o).key !== "hostile" || !radarVisible(e, o, me)) continue;
      if (now - (this.alerted.get(e.name) || 0) < 5 * 60 * 1000) continue;
      this.alerted.set(e.name, now);
      this.flash = now;
      if (o.alertsound) { beep(880); setTimeout(() => beep(660), 200); }
      if (o.alertnotify && !this.compact) {
        const text = `${e.name}${e.guild ? ` [${e.guild}]` : ""} в ${fmt(e.dist)} м · ${me.zone_name || ""}`
          + (e.ip ? ` · IP ${e.ip}` : "") + (e.role ? ` · ${e.role}` : "");
        apiPost("/api/radar/alert", { name: e.name, text }).catch(() => {});
      }
    }
  }

  // Схема зоны: запрашивается при смене зоны; пока сервер качает — повтор через 2 с.
  async syncZone(force = false) {
    const zone = this.data.me.zone || "";
    if (!zone || (!force && zone === this.zone && (!this.map || this.map.status !== "loading"))) return;
    if (this._zoneBusy) return;
    if (zone !== this.zone) {
      this.zone = zone; this.map = null; this.image = null; this.mapImg = null; this.anchor = null; this.heat = [];
      this.trails.clear();
    }
    this._zoneBusy = true;
    try {
      const m = await api("/api/zonemap", { zone });
      if (zone !== this.zone) return;
      this.map = m;
      if (m.status === "ready") this.image = renderZoneImage(m);
      else if (m.status === "loading") setTimeout(() => this.syncZone(true), 2000);
      if (m.image) {
        const im = new Image();
        im.onload = () => { if (this.zone === zone) this.mapImg = { img: im, ...m.image }; };
        im.src = m.image.url;
      }
      this.loadHeat();
    } catch (e) {
      this.map = { status: "error", error: e.message };
    } finally { this._zoneBusy = false; }
  }

  async loadHeat() {
    if (!this.opts.heat || !this.zone) return;
    if (this._heatAt && Date.now() - this._heatAt < 60000 && this._heatZone === this.zone) return;
    this._heatAt = Date.now(); this._heatZone = this.zone;
    try { this.heat = (await api("/api/radar/heat", { zone: this.zone })).cells; } catch { /* нет данных */ }
  }

  bounds() {
    if (this.mapImg && this.mapImg.bounds) return this.mapImg.bounds;
    if (this.map && this.map.status === "ready") return this.map.bounds;
    return null;
  }

  // Камера: центр (мировые координаты) и масштаб (пикселей на метр).
  camera(w, h) {
    const o = this.opts, me = this.data.me;
    if (o.mode !== "static") return { x: me.x, y: me.y, scale: o.zoom };
    let b = this.bounds();
    if (!b) {
      if (!this.anchor) this.anchor = { x: me.x, y: me.y };
      b = [this.anchor.x - 150, this.anchor.y - 150, this.anchor.x + 150, this.anchor.y + 150];
    }
    const span = Math.max(b[2] - b[0], b[3] - b[1]) * (o.rotate ? Math.SQRT2 : 1);
    return { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2,
      scale: 0.9 * Math.min(w, h) / Math.max(span, 1) * Math.max(1, Number(o.staticzoom) || 1) };
  }

  draw() {
    const cv = this.canvas, ctx = cv.getContext("2d"), o = this.opts;
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    const me = this.data.me, cam = this.camera(w, h), scale = cam.scale, cx = w / 2, cy = h / 2;
    const isStatic = o.mode === "static";
    // Мелкий масштаб (вся зона на экране): подписи только у игроков, без подробностей.
    this.dense = scale < 1.5;
    const rot = o.rotate ? -Math.PI / 4 : 0, cs = Math.cos(rot), sn = Math.sin(rot);
    const at = (x, y) => {
      const dx = x - cam.x, dy = y - cam.y;
      return [cx + (dx * cs - dy * sn) * scale, cy - (dx * sn + dy * cs) * scale];
    };
    this.at = at;
    const S = [scale * cs, -scale * sn, -scale * sn, -scale * cs,
      cx + scale * (-cs * cam.x + sn * cam.y), cy + scale * (sn * cam.x + cs * cam.y)];
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = (this.image || this.mapImg) ? ZONE_STYLE.outside : ZONE_STYLE.base;
    ctx.fillRect(0, 0, w, h);

    if (o.background) this.drawBackground(ctx, S, dpr, o);
    if (isStatic && this.bounds()) this.drawFrame(ctx, at);
    if (o.heat) this.drawHeat(ctx, at, scale);

    // Кольца расстояний — вокруг персонажа (в статичном режиме они едут вместе с ним).
    const [mx, my] = at(me.x, me.y);
    const ringStep = isStatic ? 50 : scale >= 2 ? 25 : scale >= 0.8 ? 50 : 100;
    ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.lineWidth = 1;
    ctx.fillStyle = "rgba(255,255,255,0.45)"; ctx.font = "10px system-ui, sans-serif"; ctx.textAlign = "left";
    const maxR = isStatic ? 2 * ringStep : Math.hypot(w, h) / 2 / scale;
    for (let r = ringStep; r <= maxR; r += ringStep) {
      ctx.beginPath(); ctx.arc(mx, my, r * scale, 0, 2 * Math.PI); ctx.stroke();
      if (!this.compact && (isStatic || r % (ringStep * 2) === 0)) ctx.fillText(`${r} м`, mx + r * scale + 3, my - 3);
    }
    if (o.alert && !this.dense) {   // радиус оповещения
      ctx.setLineDash([4, 4]); ctx.strokeStyle = "rgba(255,80,80,0.45)";
      ctx.beginPath(); ctx.arc(mx, my, o.alertradius * scale, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
    }

    ctx.textAlign = "center";
    const hits = [];
    this.labels = [];
    this.hits = hits;   // экранные позиции — для подсказки при наведении
    if (o.depleted) this.drawDepleted(ctx, at, o, hits);

    // Выходы из зоны с названиями соседних зон.
    if (o.exits && this.map && this.map.status === "ready") {
      ctx.font = "11px system-ui, sans-serif";
      for (const [x, y, , icon, name] of this.map.exits_world || []) {
        const [sx, sy] = at(x, y);
        if (sx < -40 || sy < -40 || sx > w + 40 || sy > h + 40) continue;
        ctx.fillStyle = "#f2d27a"; ctx.strokeStyle = "#1a1a1a"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 2 * Math.PI); ctx.stroke(); ctx.fill();
        const label = icon === "Bank" ? "банк" : icon === "Marketplace" ? "рынок" : name;
        if (label && o.labels && !this.dense) this.queueLabel(short(label, 14), sx, sy - 9, "#f2d27a", 4, 0);
        hits.push([sx, sy, { kind: "exit", name: label || "выход", dist: Math.hypot(x - me.x, y - me.y) }]);
      }
    }

    const ents = this.data.entities.filter((e) => radarVisible(e, o, me));
    if (o.trails) this.drawTrails(ctx, at, ents);
    if (o.route) this.drawRoute(ctx, at, ents, me);
    if (this.data.bot && this.data.bot.zone === me.zone) this.drawBot(ctx, at, this.data.bot, me);
    // Сначала дальние; игроки поверх остального.
    ents.sort((a, b) => (a.kind === "player") - (b.kind === "player") || b.dist - a.dist);
    const offscreen = [];
    const players = [];
    for (const e of ents) {
      const [sx, sy] = at(e.x, e.y);
      if (sx < -10 || sy < -10 || sx > w + 10 || sy > h + 10) {
        if (o.arrows && this.important(e, me)) offscreen.push([e, sx, sy]);
        continue;
      }
      const stale = o.stale && (e.kind === "player" || e.kind === "mob") && e.age > STALE_SECONDS;
      ctx.globalAlpha = stale ? 0.4 : 1;
      if (e.kind === "resource") this.drawResource(ctx, e, sx, sy);
      else if (e.kind === "player") { players.push([e, sx, sy, ctx.globalAlpha]); hits.push([sx, sy, e]); ctx.globalAlpha = 1; continue; }
      else if (e.kind === "mob") this.drawMob(ctx, e, sx, sy);
      else if (e.kind === "loot") this.drawLoot(ctx, e, sx, sy);
      else {
        const info = drawObjectIcon(ctx, e, sx, sy, this.dense);
        if (o.labels && !this.dense) this.queueLabel(radarLabel(e), sx, sy - 12, info.color, 1, e.dist);
      }
      ctx.globalAlpha = 1;
      hits.push([sx, sy, e]);
    }
    // Подписи остальных — в обход точек игроков; игроки — поверх всего.
    ctx.font = "bold 11px system-ui, sans-serif";
    const nameW = (e) => (o.labels && o.playerlabel !== "none" ? ctx.measureText(short(e.name)).width / 2 + 3 : 0);
    this.flushLabels(ctx, players.flatMap(([e, x, y]) => [[x - 9, y - 9, x + 9, y + 9],
      [x - nameW(e), y - 20, x + nameW(e), y - 5]]));
    for (const [e, sx, sy, alpha] of players) {
      ctx.globalAlpha = alpha;
      this.drawPlayer(ctx, e, sx, sy, me);
      ctx.globalAlpha = 1;
    }
    this.flushLabels(ctx);
    if (o.squads) this.drawSquads(ctx, at, ents, me);

    // Я: в статичном режиме — метка с обводкой, чтобы было видно на всей карте.
    ctx.fillStyle = "#ffffff"; ctx.strokeStyle = "#000"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(mx, my, isStatic ? 6 : 5, 0, 2 * Math.PI); ctx.stroke(); ctx.fill();
    if (isStatic) {
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(mx, my, 11, 0, 2 * Math.PI); ctx.stroke();
    }
    if (offscreen.length) this.drawArrows(ctx, offscreen, w, h, me);

    // Вспышка рамки при оповещении.
    const since = Date.now() - this.flash;
    if (since < 1500) {
      ctx.strokeStyle = `rgba(255,40,40,${0.9 * (1 - since / 1500)})`; ctx.lineWidth = 8;
      ctx.strokeRect(4, 4, w - 8, h - 8);
    }
    if (this.hud) this.drawHud(me);
  }

  drawBackground(ctx, S, dpr, o) {
    const alpha = Math.max(0.1, Math.min(1, (o.bgopacity ?? 100) / 100));
    let M = null, img = null;
    if (this.mapImg) {
      // Своя картинка: «game» — ромб как карта зоны в игре, «flat» — вид сверху без поворота.
      const mi = this.mapImg, b = this.bounds(), W = mi.img.naturalWidth, H = mi.img.naturalHeight;
      if (b && W && H) {
        img = mi.img;
        if (mi.kind === "flat") {
          M = affMul(S, [(b[2] - b[0]) / W, 0, 0, -(b[3] - b[1]) / H, b[0], b[3]]);
        } else {
          const c = Math.SQRT1_2, mxw = (b[0] + b[2]) / 2, myw = (b[1] + b[3]) / 2;
          const G = [c, c, c, -c, -c * mxw - c * myw, -c * mxw + c * myw];   // поворот −45° и y вниз
          const pts = [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]
            .map(([x, y]) => [G[0] * x + G[2] * y + G[4], G[1] * x + G[3] * y + G[5]]);
          const gx0 = Math.min(...pts.map((p) => p[0])), gx1 = Math.max(...pts.map((p) => p[0]));
          const gy0 = Math.min(...pts.map((p) => p[1])), gy1 = Math.max(...pts.map((p) => p[1]));
          M = affMul(S, affMul(affInv(G), [(gx1 - gx0) / W, 0, 0, (gy1 - gy0) / H, gx0, gy0]));
        }
      }
    }
    if (!img && this.image) {
      const { canvas, k, x0, z1 } = this.image;
      img = canvas;
      M = affMul(S, [1 / k, 0, 0, -1 / k, x0, z1]);
    }
    if (!img) return;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.setTransform(M[0] * dpr, M[1] * dpr, M[2] * dpr, M[3] * dpr, M[4] * dpr, M[5] * dpr);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(img, 0, 0);
    ctx.restore();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // Рамка зоны и стороны света, как на карте зоны в игре.
  drawFrame(ctx, at) {
    const [x0, z0, x1, z1] = this.bounds();
    const corners = [at(x0, z0), at(x1, z0), at(x1, z1), at(x0, z1)];
    ctx.save();
    ctx.lineJoin = "round";
    ctx.beginPath(); corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.closePath();
    ctx.strokeStyle = ZONE_STYLE.frameEdge; ctx.lineWidth = 14; ctx.stroke();
    ctx.strokeStyle = ZONE_STYLE.frame; ctx.lineWidth = 10; ctx.stroke();
    const pad = (x1 - x0) * 0.03;
    ctx.font = "bold 18px Georgia, serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    // Как в игре, стороны света — на углах ромба (при повороте 45°).
    for (const [t, x, y] of [["N", x0 - pad, z1 + pad], ["E", x1 + pad, z1 + pad], ["S", x1 + pad, z0 - pad],
      ["W", x0 - pad, z0 - pad]]) {
      const [sx, sy] = at(x, y);
      drawText(ctx, t, sx, sy, "#f1dfb8");
    }
    ctx.restore();
  }

  // Тепловая карта: где в этой зоне встречались ресурсы (с учётом фильтров).
  drawHeat(ctx, at, scale) {
    const o = this.opts, cells = this.heat.filter((c) => resAllowed(c.res, c.tier, c.enchant, o));
    if (!cells.length) return;
    const max = Math.max(...cells.map((c) => c.seen));
    const r = Math.max(3, 6 * scale);
    for (const c of cells) {
      const [sx, sy] = at(c.x, c.y);
      ctx.globalAlpha = 0.15 + 0.5 * Math.log1p(c.seen) / Math.log1p(max);
      ctx.fillStyle = RES_COLOR[c.res] || "#fff";
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, 2 * Math.PI); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  // Истощённые узлы: бледный значок и таймер до ожидаемого респауна.
  drawDepleted(ctx, at, o, hits) {
    const respawn = (o.respawnmin || 10) * 60;
    ctx.font = "bold 10px system-ui, sans-serif";
    for (const d of this.data.depleted || []) {
      if (!resAllowed(d.res, d.tier, d.enchant, o)) continue;
      const [sx, sy] = at(d.x, d.y);
      const left = respawn - d.ago;
      ctx.globalAlpha = 0.35;
      drawResourceIcon(ctx, d, sx, sy, this.dense ? 16 : 26);
      ctx.globalAlpha = 1;
      if (!this.dense) drawText(ctx, left > 0 ? `${Math.ceil(left / 60)}м` : "готов?", sx, sy + 14, left > 0 ? "#cfcfcf" : "#7CFC9A");
      hits.push([sx, sy, { kind: "depleted", ...d, left }]);
    }
  }

  drawTrails(ctx, at, ents) {
    const now = Date.now() / 1000;
    ctx.lineWidth = 2;
    for (const e of ents) {
      const t = this.trails.get(e.id);
      if (!t || t.length < 2) continue;
      const color = e.kind === "player" ? playerStatus(e, this.data.me, this.opts).color : RADAR_COLOR.mob;
      for (let i = 1; i < t.length; i++) {
        const [a, b] = [at(t[i - 1][1], t[i - 1][2]), at(t[i][1], t[i][2])];
        ctx.globalAlpha = Math.max(0.05, 0.8 * (1 - (now - t[i][0]) / 8));
        ctx.strokeStyle = color;
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }

  // Маршрут сбора: от вас через ближайшие ценные узлы (жадный обход).
  drawRoute(ctx, at, ents, me) {
    const path = gatheringRoute(ents, me, this.opts.routelen);
    if (!path.length) return;
    ctx.save();
    ctx.setLineDash([6, 5]); ctx.lineWidth = 2.5; ctx.strokeStyle = "rgba(255,230,120,0.9)";
    ctx.beginPath();
    const [sx, sy] = at(me.x, me.y); ctx.moveTo(sx, sy);
    path.forEach((n) => { const [x, y] = at(n.x, n.y); ctx.lineTo(x, y); });
    ctx.stroke(); ctx.setLineDash([]);
    ctx.font = "bold 10px system-ui, sans-serif";
    path.forEach((n, i) => { const [x, y] = at(n.x, n.y); drawText(ctx, String(i + 1), x - 12, y - 10, "#ffe678"); });
    ctx.restore();
  }

  // Бот: разведанные клетки, путь по схеме зоны и текущая цель.
  drawBot(ctx, at, bot, me) {
    ctx.save();
    ctx.fillStyle = "rgba(80,200,255,0.10)";
    for (const [x, y, size] of bot.explored || []) {
      const a = at(x, y), b = at(x + size, y), c = at(x + size, y + size), d = at(x, y + size);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(c[0], c[1]); ctx.lineTo(d[0], d[1]);
      ctx.closePath(); ctx.fill();
    }
    const path = bot.path || [];
    if (path.length) {
      ctx.setLineDash([3, 4]); ctx.lineWidth = 2; ctx.strokeStyle = "rgba(80,200,255,0.9)";
      ctx.beginPath();
      const [sx, sy] = at(me.x, me.y); ctx.moveTo(sx, sy);
      path.forEach(([x, y]) => { const [px, py] = at(x, y); ctx.lineTo(px, py); });
      ctx.stroke(); ctx.setLineDash([]);
    }
    if (bot.target) {
      const [tx, ty] = at(bot.target[0], bot.target[1]);
      ctx.strokeStyle = "#50c8ff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(tx - 6, ty - 6); ctx.lineTo(tx + 6, ty + 6); ctx.moveTo(tx + 6, ty - 6); ctx.lineTo(tx - 6, ty + 6);
      ctx.stroke();
      ctx.font = "bold 11px system-ui, sans-serif";
      if (bot.status) drawText(ctx, `бот: ${bot.status}`, tx, ty - 10, "#50c8ff");
    }
    ctx.restore();
  }

  // Отряды: 4+ враждебных игрока в 25 м друг от друга.
  drawSquads(ctx, at, ents, me) {
    for (const group of findSquads(ents, me, this.opts)) {
      const gx = group.reduce((s, e) => s + e.x, 0) / group.length, gy = group.reduce((s, e) => s + e.y, 0) / group.length;
      const rad = Math.max(...group.map((e) => Math.hypot(e.x - gx, e.y - gy))) + 8;
      const [sx, sy] = at(gx, gy), [ex] = at(gx + rad, gy);
      const r = Math.max(18, Math.hypot(ex - sx, at(gx + rad, gy)[1] - sy));
      ctx.strokeStyle = "rgba(255,59,59,0.85)"; ctx.lineWidth = 2; ctx.setLineDash([8, 4]);
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
      ctx.font = "bold 12px system-ui, sans-serif";
      drawText(ctx, `отряд: ${group.length}`, sx, sy - r - 6, "#ff6b6b");
    }
  }

  important(e, me) {
    if (e.kind === "player") return playerStatus(e, me, this.opts).key === "hostile";
    if (e.kind === "loot") return !e.opened && (e.rarity == null || e.rarity >= 1);
    if (e.kind === "mob") return !!(e.mob && e.mob.boss);
    if (e.kind === "resource") return (e.enchant || 0) >= 2 || (this.opts.minvalue && e.value >= this.opts.minvalue);
    return false;
  }

  // Стрелки на краю окна для важных объектов за его пределами.
  drawArrows(ctx, list, w, h, me) {
    const cx = w / 2, cy = h / 2, pad = 16;
    ctx.font = "bold 10px system-ui, sans-serif";
    for (const { e, sx, sy, n } of groupArrows(list, cx, cy).slice(0, 20)) {
      const a = Math.atan2(sy - cy, sx - cx);
      const k = Math.min((w / 2 - pad) / Math.abs(Math.cos(a) || 1e-6), (h / 2 - pad) / Math.abs(Math.sin(a) || 1e-6));
      const x = cx + Math.cos(a) * k, y = cy + Math.sin(a) * k;
      const color = e.kind === "player" ? "#ff3b3b" : e.kind === "loot" ? (CHEST_COLOR[e.rarity] || RADAR_COLOR.loot)
        : e.kind === "mob" ? RADAR_COLOR.mob : (TIER_COLOR[e.tier] || RADAR_COLOR.resource);
      ctx.save(); ctx.translate(x, y); ctx.rotate(a);
      ctx.fillStyle = color; ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-6, -6); ctx.lineTo(-3, 0); ctx.lineTo(-6, 6); ctx.closePath();
      ctx.fill(); ctx.stroke(); ctx.restore();
      const label = `${e.kind === "player" ? (n > 1 ? `×${n} ` : e.name + " ") : n > 1 ? `×${n} ` : ""}${fmt(e.dist)}м`;
      drawText(ctx, label, x - Math.cos(a) * 18, y - Math.sin(a) * 14 + 3, color);
    }
  }

  // Подписи — после значков, по важности (игроки, лут и объекты, мобы, ресурсы, выходы),
  // ближние раньше дальних; подпись, которая налезла бы на уже выведенную, не рисуется
  // (значок остаётся, полное название — в подсказке).
  queueLabel(text, x, y, color = "#f1f1f1", prio = 3, dist = 0, font = "11px system-ui, sans-serif") {
    if (text) (this.labels || (this.labels = [])).push({ text, x, y, color, prio, dist, font });
  }

  flushLabels(ctx, keepClear = []) {
    const placed = [...keepClear];
    const items = (this.labels || []).sort((a, b) => a.prio - b.prio || a.dist - b.dist);
    for (const l of items) {
      ctx.font = l.font;
      const w = ctx.measureText(l.text).width + 4, h = 12;
      const box = [l.x - w / 2, l.y - h + 2, l.x + w / 2, l.y + 3];
      if (l.prio > 0 && placed.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3])) continue;
      placed.push(box);
      drawText(ctx, l.text, l.x, l.y, l.color);
    }
    ctx.font = "11px system-ui, sans-serif";
    this.labels = [];
  }

  drawHp(ctx, e, x, y, width) {
    const f = Math.max(0, Math.min(1, e.health / e.max_health));
    ctx.fillStyle = "rgba(0,0,0,0.65)"; ctx.fillRect(x - width / 2 - 1, y - 1, width + 2, 5);
    ctx.fillStyle = f > 0.5 ? "#4cc27d" : f > 0.25 ? "#f5a524" : "#e5484d";
    ctx.fillRect(x - width / 2, y, width * f, 3);
  }

  resourceText(e) {
    const o = this.opts;
    if (o.reslabel === "value" && e.value) return `${fmtShort(e.value)}`;
    if (o.reslabel === "size" && e.size != null) return `×${e.size}`;
    return `${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}`;
  }

  drawResource(ctx, e, sx, sy) {
    const o = this.opts;
    if (o.resstyle === "text") {
      ctx.fillStyle = TIER_COLOR[e.tier] || RADAR_COLOR.resource; ctx.strokeStyle = "#111"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.rect(sx - 3, sy - 3, 6, 6); ctx.fill(); ctx.stroke();
      if (o.labels && !this.dense) this.queueLabel(radarLabel(e) + (e.value ? ` · ${fmtShort(e.value)}` : ""), sx, sy - 7, undefined, 3, e.dist);
      return;
    }
    const iid = o.iconset === "game" ? resourceItemId(e) : null;
    const img = iid ? gameIcon(iid) : null;
    const size = this.dense ? 22 : 36;
    if (img) ctx.drawImage(img, sx - size / 2, sy - size / 2, size, size);
    else drawResourceIcon(ctx, e, sx, sy, size);
    if (!o.labels || this.dense) return;
    if (o.resstyle === "both") this.queueLabel(radarLabel(e) + (e.value ? ` · ${fmtShort(e.value)}` : ""), sx, sy - size * 0.65, undefined, 3, e.dist);
    else {
      ctx.font = "bold 10px system-ui, sans-serif";
      drawText(ctx, this.resourceText(e), sx + size * 0.55, sy + size * 0.35,
        o.reslabel === "value" ? "#ffe678" : e.enchant ? ENCHANT_COLOR[e.enchant] : "#f1f1f1");
      ctx.font = "11px system-ui, sans-serif";
    }
  }

  drawMob(ctx, e, sx, sy) {
    const o = this.opts, m = e.mob, kind = mobKind(e), k = MOB_KINDS[kind];
    if (m && m.res && o.livingasres && (kind === "mob" || kind === "harmless")) {
      // живой ресурс (шкура с мобов и т. п.) — значком ресурса
      drawResourceIcon(ctx, { res: m.res, tier: m.tier, enchant: e.enchant || 0 }, sx, sy, this.dense ? 20 : 30);
      ctx.strokeStyle = RADAR_COLOR.mob; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(sx, sy, this.dense ? 9 : 13, 0, 2 * Math.PI); ctx.stroke();
    } else if (k.icon) {
      // Особые (босс, элита, кристалл, сундук, вход в Мглу) — значком в кружке.
      drawObjectIcon(ctx, { name: "", event: "" }, sx, sy, this.dense, false, k);
    } else {
      // Обычный моб — точка цвета тира (видно, какой тир, без подписи).
      ctx.fillStyle = (m && TIER_COLOR[m.tier]) || k.color; ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(sx, sy, 4, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
      if (e.enchant) {
        ctx.strokeStyle = ENCHANT_COLOR[e.enchant] || "#fff"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(sx, sy, 6.5, 0, 2 * Math.PI); ctx.stroke();
      }
    }
    const show = kind === "wisp" || o.moblabels;     // вход в Мглу подписан всегда
    if (show && !this.dense) {
      if (kind !== "wisp" && e.max_health && e.health != null && e.health < e.max_health) this.drawHp(ctx, e, sx, sy + 9, 16);
      if (o.labels) this.queueLabel(mobTitle(e), sx, sy - (k.icon ? 12 : 8), k.icon ? k.color : "#f1f1f1",
        kind === "wisp" ? 1 : 2, e.dist);
    }
  }

  drawLoot(ctx, e, sx, sy) {
    const o = this.opts, color = e.rarity != null ? CHEST_COLOR[e.rarity] : RADAR_COLOR.loot;
    drawObjectIcon(ctx, e, sx, sy, this.dense, e.opened);
    if (e.rarity != null && !e.opened) {          // редкость — цветное кольцо вокруг значка
      ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(sx, sy, this.dense ? 8 : 10.5, 0, 2 * Math.PI); ctx.stroke();
    }
    if (o.labels && !this.dense) this.queueLabel(radarLabel(e), sx, sy - 13, e.opened ? "#aaa" : color, 1, e.dist);
  }

  // Игрок компактно: точка цвета статуса, ♞ — на маунте, полоска HP — только у раненых,
  // подпись — по настройке. Остальное — в подсказке.
  drawPlayer(ctx, e, sx, sy, me) {
    const o = this.opts, st = playerStatus(e, me, o);
    const hostile = st.key === "hostile";
    if (hostile) {
      ctx.strokeStyle = "rgba(255,59,59,0.9)"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(sx, sy, 8, 0, 2 * Math.PI); ctx.stroke();
    }
    ctx.fillStyle = st.color; ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
    if (e.mounted) {
      ctx.font = "12px system-ui, sans-serif";
      drawText(ctx, "♞", sx + 11, sy + 4, "#f2d27a");
    }
    if (e.max_health && e.health != null && e.health < e.max_health * 0.995) this.drawHp(ctx, e, sx, sy + 8, 16);
    if (o.labels && o.playerlabel !== "none") {
      ctx.font = "bold 11px system-ui, sans-serif";
      let text = short(e.name);
      if (!this.dense && o.playerlabel === "guild" && e.guild) text = `${e.name} [${e.guild}]`;
      if (!this.dense && o.playerlabel === "power") text = [e.name, e.ip ? `IP ${e.ip}` : "", e.role].filter(Boolean).join(" · ");
      // Имя враждебного — всегда; остальных — если не налезает на другие имена.
      this.queueLabel(text, sx, sy - 9, hostile ? "#ff6b6b" : "#f1f1f1", hostile ? 0 : 0.5, e.dist,
        "bold 11px system-ui, sans-serif");
    }
    ctx.font = "11px system-ui, sans-serif";
  }

  // Подсказка при наведении: всё о ближайшем объекте под курсором.
  tooltip(mx, my) {
    let best = null, bd = 14 * 14;
    for (const [x, y, e] of this.hits || []) {
      const d = (x - mx) ** 2 + (y - my) ** 2;
      if (d < bd || (best && d === bd && e.kind === "player")) { bd = d; best = e; }
    }
    if (!best) return "";
    const e = best, me = this.data.me;
    const stale = e.age > STALE_SECONDS && (e.kind === "player" || e.kind === "mob")
      ? `<div class="muted">нет событий ${fmt(e.age)} с — возможно, ушёл</div>` : "";
    if (e.kind === "player") {
      const st = playerStatus(e, me, this.opts);
      const hp = e.max_health && e.health != null
        ? `${fmt(e.health)} / ${fmt(e.max_health)} (${Math.round(100 * e.health / e.max_health)}%)` : "—";
      const gear = (e.equipment || []).filter((i) => !["зелье", "еда"].includes(i.slot))
        .map((i) => `<div><span class="muted">${esc(i.slot)}:</span> ${esc(i.name || i.id)}</div>`).join("");
      const kb = e.kb ? `<div>киллборд: убийств ${fmt(e.kb.kills)}, смертей ${fmt(e.kb.deaths)}</div>` : "";
      return `<b>${esc(e.name)}</b>${e.guild ? ` [${esc(e.guild)}]` : ""}${e.alliance ? ` &lt;${esc(e.alliance)}&gt;` : ""}
        <div><span class="radar-dot" style="background:${st.color}"></span> ${esc(st.text)}</div>
        <div>HP ${hp}${e.mounted ? " · ♞ верхом" : e.mounted === false ? " · пешком" : ""} · ${fmt(e.dist)} м</div>
        ${e.ip || e.role ? `<div>${e.ip ? `сила ≈ ${fmt(e.ip)}` : ""}${e.ip && e.role ? " · " : ""}${esc(e.role || "")}</div>` : ""}
        ${kb}${gear}${stale}`;
    }
    if (e.kind === "resource") {
      return `<b>${esc(radarLabel(e, true))}</b><div class="muted">${fmt(e.dist)} м</div>`
        + (e.price ? `<div>${esc(e.item)}: ${fmt(e.price)} за шт.${e.value ? ` · узел ≈ ${fmt(e.value)}` : ""}</div>` : "");
    }
    if (e.kind === "mob") {
      return `<b>${esc(mobTitle(e, true))}</b><div class="muted">${fmt(e.dist)} м · тип #${e.type_id ?? "?"}`
        + `${e.mob ? ` → ${esc(e.mob.id)}` : " (справочник мобов не загружен)"}</div>`
        + (e.max_health && e.health != null ? `<div>HP ${fmt(e.health)} / ${fmt(e.max_health)}</div>` : "") + stale;
    }
    if (e.kind === "depleted") {
      return `<b>${esc(e.name)} T${e.tier}${e.enchant ? "." + e.enchant : ""}</b><div>истощён ${fmt(e.ago / 60)} мин назад</div>`
        + `<div class="muted">${e.left > 0 ? `респаун ≈ через ${Math.ceil(e.left / 60)} мин` : "мог уже появиться"}</div>`;
    }
    if (e.kind === "exit") return `<b>${esc(e.name)}</b><div class="muted">выход · ${fmt(e.dist)} м</div>`;
    const info = objectInfo(e);
    return `<b>${info.icon} ${esc(radarLabel(e, true))}</b><div class="muted">${fmt(e.dist)} м${e.event ? ` · ${esc(e.event)}` : ""}</div>`;
  }

  drawHud(me) {
    const n = {};
    for (const e of this.data.entities) n[e.kind] = (n[e.kind] || 0) + 1;
    const hostile = this.data.entities.filter((e) => e.kind === "player" && playerStatus(e, me, this.opts).key === "hostile").length;
    const m = this.map;
    const bg = !this.zone ? "фон: зона неизвестна (смените зону)"
      : this.mapImg ? "" : !m || m.status === "loading" ? "фон: скачиваю схему зоны…"
        : m.status === "error" ? `фон: ${m.error}` : "";
    const replay = this.data.replay ? ` · <b>запись</b> ${fmt(this.data.replay.pos)}/${fmt(this.data.replay.duration)} с` : "";
    this.hud.innerHTML = this.error ? `<span class="bad">Нет связи: ${esc(this.error)}</span>`
      : `<b>${esc(me.name || "персонаж")}</b> · ${esc(me.zone_name || "зона неизвестна")} · (${fmt1(me.x)}, ${fmt1(me.y)})${replay}<br>
        игроки ${n.player || 0}${hostile ? ` (<span style="color:#ff5a5a">враждебных ${hostile}</span>)` : ""} · мобы ${n.mob || 0}
        · ресурсы ${n.resource || 0} · лут ${n.loot || 0} · объекты ${n.object || 0}`
        + (this.opts.profile ? ` · профиль «${esc(this.opts.profile)}»` : "")
        + (bg ? `<br><span class="muted">${esc(bg)}</span>` : "");
  }
}

// Маршрут сбора: самые ценные узлы (цена, иначе тир и зачарование), обход «ближайший следующий».
function gatheringRoute(ents, me, len) {
  const score = (e) => (e.value != null ? e.value : (e.tier || 0) * 100 + (e.enchant || 0) * 300);
  let nodes = ents.filter((e) => e.kind === "resource").sort((a, b) => score(b) - score(a))
    .slice(0, Math.max(2, len * 2));
  const path = [];
  let cur = { x: me.x, y: me.y };
  while (nodes.length && path.length < len) {
    let bi = 0, bd = Infinity;
    nodes.forEach((n, i) => { const d = Math.hypot(n.x - cur.x, n.y - cur.y); if (d < bd) { bd = d; bi = i; } });
    cur = nodes.splice(bi, 1)[0];
    path.push(cur);
  }
  return path;
}

// Отряды: группы из 4+ враждебных игроков, где каждый не дальше 25 м от кого-то из группы.
function findSquads(ents, me, o, minSize = 4, gap = 25) {
  const hostile = ents.filter((e) => e.kind === "player" && playerStatus(e, me, o).key === "hostile");
  const seen = new Set(), out = [];
  for (const p of hostile) {
    if (seen.has(p.id)) continue;
    const group = [p]; seen.add(p.id);
    for (let i = 0; i < group.length; i++) {
      for (const q of hostile) {
        if (!seen.has(q.id) && Math.hypot(q.x - group[i].x, q.y - group[i].y) < gap) { seen.add(q.id); group.push(q); }
      }
    }
    if (group.length >= minSize) out.push(group);
  }
  return out;
}

// Стрелки за краем: объекты одного вида в одном направлении (±10°) — одна стрелка «×N»
// к ближайшему из них.
function groupArrows(list, cx, cy, tol = 0.18) {
  const groups = [];
  for (const [e, sx, sy] of list) {
    const a = Math.atan2(sy - cy, sx - cx);
    const g = groups.find((x) => x.kind === e.kind && Math.abs(Math.atan2(Math.sin(a - x.a), Math.cos(a - x.a))) < tol);
    if (g) { g.n++; if (e.dist < g.e.dist) Object.assign(g, { e, sx, sy }); } else groups.push({ e, sx, sy, a, kind: e.kind, n: 1 });
  }
  return groups;
}

function fmtShort(v) {
  if (v == null) return "—";
  return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}k` : String(Math.round(v));
}
