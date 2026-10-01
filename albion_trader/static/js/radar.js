"use strict";
// Радар: игроки, мобы, ресурсы и лут вокруг персонажа (события New…, Move, Leave, …Object)
// на фоне схемы текущей зоны. Отрисовка (RadarView) общая для вкладки и окна radar.html;
// настройки задаются на вкладке и хранятся в localStorage — окно подхватывает их сразу.
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
const RADAR_DEFAULTS = {
  player: true, mob: true, resource: true, loot: true, object: true,
  mode: "follow", zoom: 4, staticzoom: 1, rotate: true, labels: true, background: true, bgopacity: 100, exits: true,
  resstyle: "icon", iconset: "drawn", mintier: 1,
  passive: true, factional: true, hostile: true, playerlabel: "name", moblabels: false, myguild: "", myalliance: "",
};

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
// Флаги фракций (NewCharacter[53]) — цвета городов.
const FLAG_COLOR = { 1: "#3c7dd9", 2: "#4caf50", 3: "#f08c2e", 4: "#e8e8e8", 5: "#8e5bd6", 6: "#c62828" };
const STATUS_STYLE = {
  friend: { text: "свой", color: "#4aa3ff" },
  hostile: { text: "враждебный", color: "#ff3b3b" },
  faction: { text: "фракция", color: "#f5a524" },
  passive: { text: "мирный", color: "#9be29b" },
};

function radarOptions() { return { ...RADAR_DEFAULTS, ...loadSettings(RADAR_KEY) }; }

// Статус игрока: свой (гильдия/альянс из настроек), враждебный (флаг 255, или красная/чёрная
// зона — там напасть может любой), фракционный (флаг города 1–6) или мирный.
function playerStatus(e, me, o) {
  const same = (a, b) => a && b && a.trim().toLowerCase() === b.trim().toLowerCase();
  if (same(e.guild, o.myguild) || same(e.alliance, o.myalliance)) return { key: "friend", ...STATUS_STYLE.friend };
  if (e.faction === 255) return { key: "hostile", text: "враждебный (флаг)", color: STATUS_STYLE.hostile.color };
  const zt = (me.zone_type || "").toUpperCase();
  if (zt.includes("BLACK")) return { key: "hostile", text: "чёрная зона", color: STATUS_STYLE.hostile.color };
  if (zt.includes("RED")) return { key: "hostile", text: "красная зона", color: STATUS_STYLE.hostile.color };
  if (e.faction >= 1 && e.faction <= 6) {
    return { key: "faction", text: `фракция: ${e.flag}`, color: FLAG_COLOR[e.faction] };
  }
  return { key: "passive", ...STATUS_STYLE.passive };
}

function radarLabel(e) {
  if (e.kind === "player") return e.name + (e.guild ? ` [${e.guild}]` : "") + (e.alliance ? ` <${e.alliance}>` : "");
  if (e.kind === "resource") {
    return `${e.name} T${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}${e.size != null ? " ×" + e.size : ""}`;
  }
  if (e.kind === "mob") return (e.name || `моб #${e.type_id ?? "?"}`) + (e.enchant ? ` .${e.enchant}` : "");
  return e.name || e.event;
}

function radarVisible(e, o, me) {
  if (!o[e.kind]) return false;
  if (e.kind === "resource") return !(e.tier != null && e.tier < o.mintier);
  if (e.kind === "player") {
    const k = playerStatus(e, me || {}, o).key;
    return k === "hostile" ? o.hostile : k === "faction" ? o.factional : o.passive;
  }
  return true;
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

class RadarView {
  constructor(canvas, { hud = null, compact = false } = {}) {
    this.canvas = canvas;
    this.hud = hud;
    this.compact = compact;
    this.opts = radarOptions();
    this.data = { me: { x: 0, y: 0 }, entities: [], codes: [] };
    this.zone = null;        // id зоны, для которой загружена/грузится схема
    this.map = null;         // {status, ...схема} с сервера
    this.image = null;       // отрисованная схема
    this.anchor = null;      // центр статичной карты, если схемы зоны нет
    window.addEventListener("storage", (ev) => { if (ev.key === RADAR_KEY) this.opts = radarOptions(); });
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

  async poll() {
    try {
      this.data = await api("/api/radar");
      this.error = null;
      this.syncZone();
    } catch (e) { this.error = e.message; }
    return this.data;
  }

  // Схема зоны: запрашивается при смене зоны; пока сервер качает — повтор через 2 с.
  async syncZone(force = false) {
    const zone = this.data.me.zone || "";
    if (!zone || (!force && zone === this.zone && (!this.map || this.map.status !== "loading"))) return;
    if (this._zoneBusy) return;
    if (zone !== this.zone) { this.zone = zone; this.map = null; this.image = null; this.anchor = null; }
    this._zoneBusy = true;
    try {
      const m = await api("/api/zonemap", { zone });
      if (zone !== this.zone) return;
      this.map = m;
      if (m.status === "ready") this.image = renderZoneImage(m);
      else if (m.status === "loading") setTimeout(() => this.syncZone(true), 2000);
    } catch (e) {
      this.map = { status: "error", error: e.message };
    } finally { this._zoneBusy = false; }
  }

  // Камера: центр (мировые координаты) и масштаб (пикселей на метр).
  camera(w, h) {
    const o = this.opts, me = this.data.me;
    if (o.mode !== "static") return { x: me.x, y: me.y, scale: o.zoom };
    let b;
    if (this.map && this.map.status === "ready") b = this.map.bounds;
    else {
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
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = this.image ? ZONE_STYLE.outside : ZONE_STYLE.base;
    ctx.fillRect(0, 0, w, h);

    // Фон: картинка схемы в координатах зоны → экран (сдвиг, масштаб, поворот).
    if (o.background && this.image) {
      const { canvas: img, k, x0, z1 } = this.image;
      const a = scale * cs / k, b = -scale * sn / k, c = scale * sn / k, d = scale * cs / k;
      const [ex, ey] = at(x0, z1);
      ctx.save();
      ctx.globalAlpha = Math.max(0.1, Math.min(1, (o.bgopacity ?? 100) / 100));
      ctx.setTransform(a * dpr, b * dpr, c * dpr, d * dpr, ex * dpr, ey * dpr);
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(img, 0, 0);
      ctx.restore();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (isStatic) this.drawFrame(ctx, at);
    }

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

    ctx.textAlign = "center";
    const exitHits = [];
    // Выходы из зоны с названиями соседних зон.
    if (o.exits && this.map && this.map.status === "ready") {
      ctx.font = "11px system-ui, sans-serif";
      for (const [x, y, , icon, name] of this.map.exits_world || []) {
        const [sx, sy] = at(x, y);
        if (sx < -40 || sy < -40 || sx > w + 40 || sy > h + 40) continue;
        ctx.fillStyle = "#f2d27a"; ctx.strokeStyle = "#1a1a1a"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 2 * Math.PI); ctx.stroke(); ctx.fill();
        const label = icon === "Bank" ? "банк" : icon === "Marketplace" ? "рынок" : name;
        if (label && o.labels && !this.dense) drawText(ctx, label, sx, sy - 9, "#f2d27a");
        exitHits.push([sx, sy, { kind: "exit", name: label || "выход", dist: Math.hypot(x - me.x, y - me.y) }]);
      }
    }

    ctx.font = "11px system-ui, sans-serif";
    const ents = this.data.entities.filter((e) => radarVisible(e, o, me));
    this.hits = exitHits;   // экранные позиции — для подсказки при наведении
    // Сначала дальние; игроки поверх остального.
    ents.sort((a, b) => (a.kind === "player") - (b.kind === "player") || b.dist - a.dist);
    for (const e of ents) {
      const [sx, sy] = at(e.x, e.y);
      if (sx < -40 || sy < -40 || sx > w + 40 || sy > h + 40) continue;
      if (e.kind === "resource") this.drawResource(ctx, e, sx, sy);
      else if (e.kind === "player") this.drawPlayer(ctx, e, sx, sy, me);
      else {
        ctx.fillStyle = RADAR_COLOR[e.kind]; ctx.strokeStyle = "rgba(0,0,0,0.7)"; ctx.lineWidth = 1.5;
        ctx.beginPath();
        if (e.kind === "loot") { ctx.moveTo(sx, sy - 5); ctx.lineTo(sx + 5, sy); ctx.lineTo(sx, sy + 5); ctx.lineTo(sx - 5, sy); ctx.closePath(); }
        else ctx.arc(sx, sy, e.kind === "mob" ? 4 : 3.5, 0, 2 * Math.PI);
        ctx.stroke(); ctx.fill();
        if (e.kind === "mob" && o.moblabels && e.max_health && e.health != null && !this.dense) this.drawHp(ctx, e, sx, sy + 6, 16);
        if (o.labels && !this.dense && (e.kind !== "mob" || o.moblabels)) drawText(ctx, radarLabel(e), sx, sy - 8);
      }
      this.hits.push([sx, sy, e]);
    }

    // Я: в статичном режиме — метка с обводкой, чтобы было видно на всей карте.
    ctx.fillStyle = "#ffffff"; ctx.strokeStyle = "#000"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(mx, my, isStatic ? 6 : 5, 0, 2 * Math.PI); ctx.stroke(); ctx.fill();
    if (isStatic) {
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(mx, my, 11, 0, 2 * Math.PI); ctx.stroke();
    }

    if (this.hud) this.drawHud(me);
  }

  // Рамка зоны и стороны света, как на карте зоны в игре (север — +y зоны).
  drawFrame(ctx, at) {
    const [x0, z0, x1, z1] = this.map.bounds;
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

  drawHp(ctx, e, x, y, width) {
    const f = Math.max(0, Math.min(1, e.health / e.max_health));
    ctx.fillStyle = "rgba(0,0,0,0.65)"; ctx.fillRect(x - width / 2 - 1, y - 1, width + 2, 5);
    ctx.fillStyle = f > 0.5 ? "#4cc27d" : f > 0.25 ? "#f5a524" : "#e5484d";
    ctx.fillRect(x - width / 2, y, width * f, 3);
  }

  drawResource(ctx, e, sx, sy) {
    const o = this.opts;
    if (o.resstyle === "text") {
      ctx.fillStyle = TIER_COLOR[e.tier] || RADAR_COLOR.resource; ctx.strokeStyle = "#111"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.rect(sx - 3, sy - 3, 6, 6); ctx.fill(); ctx.stroke();
      if (o.labels && !this.dense) drawText(ctx, radarLabel(e), sx, sy - 7);
      return;
    }
    const iid = o.iconset === "game" ? resourceItemId(e) : null;
    const img = iid ? gameIcon(iid) : null;
    const size = this.dense ? 22 : 36;
    if (img) ctx.drawImage(img, sx - size / 2, sy - size / 2, size, size);
    else drawResourceIcon(ctx, e, sx, sy, size);
    if (!o.labels || this.dense) return;
    if (o.resstyle === "both") drawText(ctx, radarLabel(e), sx, sy - size * 0.65);
    else {
      ctx.font = "bold 10px system-ui, sans-serif";
      drawText(ctx, `${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}`, sx + size * 0.55, sy + size * 0.35,
        e.enchant ? ENCHANT_COLOR[e.enchant] : "#f1f1f1");
      ctx.font = "11px system-ui, sans-serif";
    }
  }

  // Игрок компактно: точка цвета статуса (у фракционных — цвет города), ♞ — на маунте,
  // полоска HP — только у раненых, подпись — имя (или имя и гильдия). Остальное — в подсказке.
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
      const text = o.playerlabel === "guild" && e.guild && !this.dense ? `${e.name} [${e.guild}]` : e.name;
      drawText(ctx, text, sx, sy - 9, hostile ? "#ff6b6b" : "#f1f1f1");
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
    if (e.kind === "player") {
      const st = playerStatus(e, me, this.opts);
      const hp = e.max_health && e.health != null
        ? `${fmt(e.health)} / ${fmt(e.max_health)} (${Math.round(100 * e.health / e.max_health)}%)` : "—";
      const gear = (e.equipment || []).filter((i) => !["зелье", "еда"].includes(i.slot))
        .map((i) => `<div><span class="muted">${esc(i.slot)}:</span> ${esc(i.name || i.id)}</div>`).join("");
      return `<b>${esc(e.name)}</b>${e.guild ? ` [${esc(e.guild)}]` : ""}${e.alliance ? ` &lt;${esc(e.alliance)}&gt;` : ""}
        <div><span class="radar-dot" style="background:${st.color}"></span> ${esc(st.text)}</div>
        <div>HP ${hp}${e.mounted ? " · ♞ верхом" : e.mounted === false ? " · пешком" : ""} · ${fmt(e.dist)} м</div>${gear}`;
    }
    if (e.kind === "exit") return `<b>${esc(e.name)}</b><div class="muted">выход · ${fmt(e.dist)} м</div>`;
    return `<b>${esc(radarLabel(e))}</b><div class="muted">${fmt(e.dist)} м</div>`;
  }

  drawHud(me) {
    const n = {};
    for (const e of this.data.entities) n[e.kind] = (n[e.kind] || 0) + 1;
    const hostile = this.data.entities.filter((e) => e.kind === "player" && playerStatus(e, me, this.opts).key === "hostile").length;
    const m = this.map;
    const bg = !this.zone ? "фон: зона неизвестна (смените зону)"
      : !m || m.status === "loading" ? "фон: скачиваю схему зоны…"
        : m.status === "error" ? `фон: ${m.error}` : "";
    this.hud.innerHTML = this.error ? `<span class="bad">Нет связи: ${esc(this.error)}</span>`
      : `<b>${esc(me.name || "персонаж")}</b> · ${esc(me.zone_name || "зона неизвестна")} · (${fmt1(me.x)}, ${fmt1(me.y)})<br>
        игроки ${n.player || 0}${hostile ? ` (<span style="color:#ff5a5a">враждебных ${hostile}</span>)` : ""} · мобы ${n.mob || 0}
        · ресурсы ${n.resource || 0} · лут ${n.loot || 0} · объекты ${n.object || 0}`
        + (bg ? `<br><span class="muted">${esc(bg)}</span>` : "");
  }
}

if (typeof App !== "undefined" && document.getElementById("groups")) App.tab({
  id: "radar", group: "world", title: "Радар",
  init(el) {
    const o = radarOptions();
    const check = (k, t) => `<label><input type="checkbox" data-opt="${k}"${o[k] ? " checked" : ""}> ${t}</label>`;
    const select = (k, t, opts) => `<label>${t} <select data-opt="${k}">${opts.map(([v, n]) =>
      `<option value="${v}"${String(o[k]) === v ? " selected" : ""}>${n}</option>`).join("")}</select></label>`;
    const text = (k, t) => `<label>${t} <input type="text" data-opt="${k}" value="${esc(o[k] || "")}"></label>`;
    const layer = (k) => `<label><input type="checkbox" data-opt="${k.kind}"${o[k.kind] ? " checked" : ""}>
      <span class="radar-dot" style="background:${k.color}"></span>${k.title}</label>`;
    el.innerHTML = `
      <p class="muted intro">Объекты вокруг персонажа из трафика игры на фоне схемы текущей зоны. Схема строится из
        раскладки уровня игры (ao-bin-dumps) и скачивается при первом входе в зону, дальше — из
        <code>data/zonemaps</code>. Настройки ниже действуют и здесь, и в отдельном окне радара.</p>
      <div class="radar">
        <div class="radar-map"><canvas id="radar-canvas"></canvas><div class="radar-hud" id="radar-hud"></div></div>
        <div class="radar-side">
          <div class="radar-actions">
            <button type="button" id="radar-open">Окно радара</button>
            <label title="Держать окно радара поверх игры (Windows)"><input type="checkbox" id="radar-pin"${o.pin ? " checked" : ""}> поверх игры</label>
          </div>
          <span class="muted" id="radar-pin-hint"></span>
          ${select("mode", "Карта", [["follow", "за персонажем"], ["static", "статичная, вся зона"]])}
          <label data-show="follow">Масштаб <input type="range" min="1" max="20" step="0.5" data-opt="zoom" value="${o.zoom}"></label>
          <label data-show="static">Приближение <input type="range" min="1" max="4" step="0.25" data-opt="staticzoom" value="${o.staticzoom}"></label>
          <div class="radar-layers">${RADAR_KINDS.map(layer).join("")}</div>
          <div class="radar-layers">
            ${check("hostile", '<span class="radar-dot" style="background:#ff3b3b"></span>враждебные')}
            ${check("factional", '<span class="radar-dot" style="background:#f5a524"></span>фракция')}
            ${check("passive", '<span class="radar-dot" style="background:#9be29b"></span>мирные')}
          </div>
          <details class="radar-more"><summary>Ещё настройки</summary>
            ${select("playerlabel", "Подпись игрока", [["name", "имя"], ["guild", "имя и гильдия"], ["none", "без подписи"]])}
            ${select("resstyle", "Ресурсы", [["icon", "значок"], ["text", "надпись"], ["both", "значок и надпись"]])}
            ${select("iconset", "Значки", [["drawn", "свои (без интернета)"], ["game", "из игры (render.albiononline.com)"]])}
            <label>Мин. тир ресурсов <input type="number" min="1" max="8" data-opt="mintier" value="${o.mintier}"></label>
            ${text("myguild", "Моя гильдия")}
            ${text("myalliance", "Мой альянс")}
            ${check("labels", "Подписи")}
            ${check("moblabels", "Названия мобов")}
            ${check("exits", "Выходы из зоны")}
            ${check("rotate", "Поворот 45° (как камера игры)")}
            ${check("background", "Фон — схема зоны")}
            <label>Яркость фона <input type="range" min="10" max="100" data-opt="bgopacity" value="${o.bgopacity}"></label>
          </details>
          <p class="muted small-hint">Наведите курсор на игрока — статус, HP, маунт и снаряжение.</p>
          <h2>Рядом</h2>
          <div class="radar-list" id="radar-list"></div>
        </div>
      </div>
      <h2>Игроки рядом</h2>
      <div id="radar-players"></div>
      <details class="radar-codes"><summary>Коды событий (диагностика)</summary>
        <p class="muted">Все события, что пришли от сервера: код, имя (если известно), количество и параметры с типами.
          <code>·pos</code> — значение похоже на координаты. Если на радаре пусто, по этой таблице видно, под каким
          кодом приходят, например, мобы, — его можно указать в <code>data/opcodes.json</code>:
          <code>{"events": {"new_mob": 126}}</code>.</p>
        <div id="radar-codes"></div>
      </details>`;
    this.view = new RadarView($("#radar-canvas", el), { hud: $("#radar-hud", el) });
    const modeRows = () => $$("[data-show]", el).forEach((r) => { r.hidden = r.dataset.show !== this.view.opts.mode; });
    $$("[data-opt]", el).forEach((inp) => inp.addEventListener(inp.type === "text" ? "change" : "input", () => {
      const s = radarOptions();
      s[inp.dataset.opt] = inp.type === "checkbox" ? inp.checked
        : (inp.type === "range" || inp.type === "number") ? Number(inp.value) : inp.value;
      saveSettings(s, RADAR_KEY);
      this.view.opts = s;
      modeRows();
      this.renderList();
    }));
    modeRows();
    $("#radar-open", el).addEventListener("click", async () => {
      try {
        const r = await apiPost("/api/window", { which: "radar", action: "open" });
        if (r.mode) return;
      } catch { /* не этот компьютер — всплывающее окно браузера */ }
      window.open("radar.html", "albion-radar", "popup,width=520,height=520");
    });
    $("#radar-pin", el).addEventListener("change", async (ev) => {
      const on = ev.target.checked;
      saveSettings({ ...radarOptions(), pin: on }, RADAR_KEY);
      try {
        const r = await apiPost("/api/window", { which: "radar", topmost: on });
        $("#radar-pin-hint").textContent = on && !r.applied ? (r.reason || "") : "";
      } catch { $("#radar-pin-hint").textContent = on ? "доступно на компьютере с программой" : ""; }
    });
    this.players = makeTable($("#radar-players", el), [
      { key: "name", title: "Игрок", html: (r) => `<b>${esc(r.name)}</b>` + (r.guild || r.alliance
        ? ` <span class="muted">${r.guild ? `[${esc(r.guild)}]` : ""}${r.alliance ? ` &lt;${esc(r.alliance)}&gt;` : ""}</span>` : "") },
      { key: "status", title: "Статус", sort: (r) => r._st.key,
        html: (r) => `<span class="radar-dot" style="background:${r._st.color}"></span> ${esc(r._st.text)}` },
      { key: "hp", title: "HP", sort: (r) => (r.max_health ? r.health / r.max_health : -1),
        html: (r) => (r.max_health && r.health != null ? `${Math.round(100 * r.health / r.max_health)}%` : "—") },
      { key: "mounted", title: "♞", sort: (r) => (r.mounted ? 1 : 0), html: (r) => (r.mounted ? "♞" : "") },
      { key: "weapon", title: "Оружие", sort: (r) => r._weapon,
        html: (r) => `<span title="${esc((r.equipment || []).map((i) => `${i.slot}: ${i.name || i.id}`).join("\n"))}">${esc(r._weapon || "—")}</span>` },
      { key: "dist", title: "м", html: (r) => fmt(r.dist) },
    ], { sort: "dist", asc: true, empty: "Игроков рядом нет." });
    const frame = () => { if (App.current === this) this.view.draw(); requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
    setInterval(() => { if (App.current === this && !document.hidden) this.poll(); }, 300);
    setInterval(() => { if (App.current === this && !document.hidden) { this.renderCodes(); this.renderPlayers(); } }, 1500);
    this.poll().then(() => this.renderPlayers());
  },
  show() { if (this.view) this.poll(); },

  async poll() {
    await this.view.poll();
    this.renderList();
  },

  renderPlayers() {
    const me = this.view.data.me, o = this.view.opts;
    this.players.set(this.view.data.entities.filter((e) => e.kind === "player")
      .map((e) => ({ ...e, _st: playerStatus(e, me, o),
        _weapon: ((e.equipment || []).find((i) => i.slot === "оружие") || {}).name || "" })));
  },

  renderList() {
    const o = this.view.opts, me = this.view.data.me;
    const rows = this.view.data.entities.filter((e) => e.kind !== "player" && radarVisible(e, o, me)).slice(0, 60);
    $("#radar-list").innerHTML = rows.length ? rows.map((e) =>
      `<div><span class="radar-dot" style="background:${e.kind === "resource" ? (TIER_COLOR[e.tier] || RADAR_COLOR.resource) : RADAR_COLOR[e.kind]}"></span>
        <span>${esc(radarLabel(e))}</span><span class="muted">${fmt(e.dist)} м</span></div>`).join("")
      : '<p class="muted">Никого. Смените зону, чтобы сборщик увидел объекты вокруг.</p>';
  },

  renderCodes() {
    const codes = this.view.data.codes || [];
    $("#radar-codes").innerHTML = codes.length ? `<div class="table-wrap"><table><thead><tr><th>Код</th><th>Имя</th>
      <th>Кол-во</th><th>Параметры</th></tr></thead><tbody>${codes.map((c) => `<tr><td>${c.code}</td>
      <td>${esc(c.name || "—")}</td><td>${fmt(c.count)}</td><td><code>${esc(c.shape)}</code></td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">Событий пока не было.</p>';
  },
});
