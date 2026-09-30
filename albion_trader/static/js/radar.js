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
  passive: true, factional: true, hostile: true, playerinfo: true, equipment: false, myguild: "", myalliance: "",
};

// Цвета схемы зоны (как у мини-карты игры — одинаковые в светлой и тёмной теме).
const ZONE_STYLE = {
  base: "#28332b", low: [44, 58, 42], high: [104, 120, 76],
  water: "#2d6f93", road: "#9a8759", cliff: "rgba(12,15,13,0.7)", building: "#6e5438",
  plot: "rgba(255,255,255,0.07)",
  rock: "#6f7470", tree: "#1d4a2a",
};
const ZONE_ORDER = ["ground", "water", "plot", "road", "cliff", "building", "rock", "tree"];

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

// Свой значок ресурса: форма — вид (дерево, камень, волокно, шкура, руда), цвет — тир,
// точки снизу — зачарование.
function drawResourceIcon(ctx, e, x, y, s = 7) {
  ctx.save();
  ctx.fillStyle = TIER_COLOR[e.tier] || RADAR_COLOR.resource;
  ctx.strokeStyle = "#111"; ctx.lineWidth = 1.3; ctx.lineJoin = "round";
  ctx.beginPath();
  switch (e.res) {
    case "wood":   // ёлка + ствол
      ctx.moveTo(x, y - s); ctx.lineTo(x + s * 0.85, y + s * 0.45); ctx.lineTo(x - s * 0.85, y + s * 0.45); ctx.closePath();
      ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.fillStyle = "#6b4a2b"; ctx.rect(x - s * 0.18, y + s * 0.45, s * 0.36, s * 0.55);
      break;
    case "rock":   // неровный камень
      ctx.moveTo(x - s * 0.9, y + s * 0.6); ctx.lineTo(x - s * 0.7, y - s * 0.3); ctx.lineTo(x - s * 0.1, y - s * 0.85);
      ctx.lineTo(x + s * 0.75, y - s * 0.45); ctx.lineTo(x + s * 0.95, y + s * 0.6); ctx.closePath();
      break;
    case "fiber":  // три листа
      for (const a of [-0.55, 0, 0.55]) {
        ctx.moveTo(x, y + s);
        ctx.ellipse(x + Math.sin(a) * s * 0.55, y - Math.cos(a) * s * 0.2, s * 0.28, s * 0.8, a, 0, 2 * Math.PI);
      }
      break;
    case "hide":   // растянутая шкура
      ctx.moveTo(x - s * 0.5, y - s); ctx.quadraticCurveTo(x, y - s * 0.6, x + s * 0.5, y - s);
      ctx.lineTo(x + s * 0.95, y - s * 0.35); ctx.quadraticCurveTo(x + s * 0.55, y, x + s * 0.95, y + s * 0.4);
      ctx.lineTo(x + s * 0.45, y + s); ctx.quadraticCurveTo(x, y + s * 0.65, x - s * 0.45, y + s);
      ctx.lineTo(x - s * 0.95, y + s * 0.4); ctx.quadraticCurveTo(x - s * 0.55, y, x - s * 0.95, y - s * 0.35);
      ctx.closePath();
      break;
    case "ore":    // кристалл
      ctx.moveTo(x, y - s); ctx.lineTo(x + s * 0.75, y - s * 0.2); ctx.lineTo(x + s * 0.4, y + s);
      ctx.lineTo(x - s * 0.4, y + s); ctx.lineTo(x - s * 0.75, y - s * 0.2); ctx.closePath();
      ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x, y - s); ctx.lineTo(x, y + s); ctx.moveTo(x - s * 0.75, y - s * 0.2);
      ctx.lineTo(x + s * 0.75, y - s * 0.2); ctx.stroke();
      ctx.restore();
      drawEnchantPips(ctx, e.enchant, x, y + s + 3);
      return;
    default:
      ctx.rect(x - s * 0.6, y - s * 0.6, s * 1.2, s * 1.2);
  }
  ctx.fill(); ctx.stroke();
  ctx.restore();
  drawEnchantPips(ctx, e.enchant, x, y + s + 3);
}

function drawEnchantPips(ctx, n, x, y) {
  if (!n) return;
  ctx.save();
  ctx.fillStyle = ENCHANT_COLOR[n] || "#fff"; ctx.strokeStyle = "#111"; ctx.lineWidth = 1;
  for (let i = 0; i < n; i++) {
    ctx.beginPath(); ctx.arc(x + (i - (n - 1) / 2) * 5, y, 2, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
  }
  ctx.restore();
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
      } else ctx.fillRect(-tw * k / 2, -th * k / 2, tw * k, th * k);
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
      scale: Math.min(w, h) / Math.max(span, 1) * Math.max(1, Number(o.staticzoom) || 1) };
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
    ctx.fillStyle = ZONE_STYLE.base;
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
    // Выходы из зоны с названиями соседних зон.
    if (o.exits && this.map && this.map.status === "ready") {
      ctx.font = "11px system-ui, sans-serif";
      for (const [x, y, , icon, name] of this.map.exits_world || []) {
        const [sx, sy] = at(x, y);
        if (sx < -40 || sy < -40 || sx > w + 40 || sy > h + 40) continue;
        ctx.fillStyle = "#f2d27a"; ctx.strokeStyle = "#1a1a1a"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 2 * Math.PI); ctx.stroke(); ctx.fill();
        const label = icon === "Bank" ? "банк" : icon === "Marketplace" ? "рынок" : name;
        if (label && o.labels) drawText(ctx, label, sx, sy - 9, "#f2d27a");
      }
    }

    ctx.font = "11px system-ui, sans-serif";
    const ents = this.data.entities.filter((e) => radarVisible(e, o, me));
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
        if (e.kind === "mob" && e.max_health && e.health != null && !this.dense) this.drawHp(ctx, e, sx, sy + 6, 16);
        if (o.labels && !this.dense) drawText(ctx, radarLabel(e), sx, sy - 8);
      }
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
    if (img) ctx.drawImage(img, sx - 11, sy - 11, 22, 22);
    else drawResourceIcon(ctx, e, sx, sy, this.dense ? 5 : 7);
    if (!o.labels || this.dense) return;
    if (o.resstyle === "both") drawText(ctx, radarLabel(e), sx, sy - 12);
    else drawText(ctx, `${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}`, sx + 12, sy + 4);
  }

  drawPlayer(ctx, e, sx, sy, me) {
    const o = this.opts, st = playerStatus(e, me, o);
    // Кольцо — статус (враждебный — красное и толще), точка — цвет флага фракции.
    ctx.lineWidth = st.key === "hostile" ? 3 : 2; ctx.strokeStyle = st.color;
    ctx.beginPath(); ctx.arc(sx, sy, 7, 0, 2 * Math.PI); ctx.stroke();
    ctx.fillStyle = FLAG_COLOR[e.faction] || (st.key === "friend" ? st.color : RADAR_COLOR.player);
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.arc(sx, sy, 4.5, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
    if (e.mounted) drawText(ctx, "♞", sx + 12, sy + 4, "#f2d27a");
    if (e.max_health && e.health != null) this.drawHp(ctx, e, sx, sy + 10, 24);
    if (!o.labels) return;
    ctx.font = "bold 11px system-ui, sans-serif";
    drawText(ctx, this.dense ? e.name : radarLabel(e), sx, sy - 11, st.color);
    ctx.font = "10px system-ui, sans-serif";
    let y = sy + 24;
    if (this.dense) { ctx.font = "11px system-ui, sans-serif"; return; }
    if (o.playerinfo) {
      const hp = e.max_health && e.health != null ? `HP ${fmt(e.health)}/${fmt(e.max_health)}` : "";
      const weapon = (e.equipment || []).find((i) => i.slot === "оружие");
      const info = [st.text, e.mounted ? "верхом" : "", hp, weapon ? weapon.name : ""].filter(Boolean).join(" · ");
      drawText(ctx, info, sx, y);
      y += 6;
    }
    if (o.equipment && o.iconset === "game" && e.equipment && e.equipment.length) {
      const items = e.equipment.filter((i) => ["оружие", "вторая рука", "голова", "броня", "обувь", "плащ"].includes(i.slot));
      const size = 16, x0 = sx - (items.length * size) / 2;
      items.forEach((it, i) => { const im = gameIcon(it.id); if (im) ctx.drawImage(im, x0 + i * size, y, size, size); });
    }
    ctx.font = "11px system-ui, sans-serif";
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
      <span class="radar-dot" style="background:${k.color}"></span><b>${k.title}</b></label>`;
    el.innerHTML = `
      <p class="muted intro">Объекты вокруг персонажа из трафика игры на фоне схемы текущей зоны. Схема строится из
        раскладки уровня игры (ao-bin-dumps) и скачивается при первом входе в зону, дальше — из
        <code>data/zonemaps</code>. Настройки ниже действуют и здесь, и в отдельном окне радара.</p>
      <div class="radar">
        <div class="radar-map"><canvas id="radar-canvas"></canvas><div class="radar-hud" id="radar-hud"></div></div>
        <div class="radar-side">
          <fieldset class="radar-opts"><legend>Окно радара</legend>
            <button type="button" id="radar-open">Открыть окно радара</button>
            <label><input type="checkbox" id="radar-pin"${o.pin ? " checked" : ""}> Поверх игры (Windows)</label>
            <span class="muted" id="radar-pin-hint"></span>
          </fieldset>
          <fieldset class="radar-opts"><legend>Карта</legend>
            ${select("mode", "Вид", [["follow", "за персонажем"], ["static", "статичная карта зоны"]])}
            <label data-show="follow">Масштаб <input type="range" min="1" max="20" step="0.5" data-opt="zoom" value="${o.zoom}"></label>
            <label data-show="static">Приближение <input type="range" min="1" max="4" step="0.25" data-opt="staticzoom" value="${o.staticzoom}"></label>
            ${check("rotate", "Поворот 45° (как камера игры)")}
            ${check("background", "Фон — схема зоны")}
            <label>Яркость фона <input type="range" min="10" max="100" data-opt="bgopacity" value="${o.bgopacity}"></label>
            ${check("exits", "Выходы из зоны")}
            ${check("labels", "Подписи")}
          </fieldset>
          <fieldset class="radar-opts"><legend>Ресурсы</legend>
            ${layer(RADAR_KINDS[2])}
            ${select("resstyle", "Показывать", [["icon", "значок"], ["text", "надпись"], ["both", "значок и надпись"]])}
            ${select("iconset", "Значки", [["drawn", "свои (без интернета)"], ["game", "из игры (render.albiononline.com)"]])}
            <label>Мин. тир <input type="number" min="1" max="8" data-opt="mintier" value="${o.mintier}"></label>
          </fieldset>
          <fieldset class="radar-opts"><legend>Игроки</legend>
            ${layer(RADAR_KINDS[0])}
            ${check("hostile", '<span style="color:#ff3b3b">●</span> враждебные')}
            ${check("factional", '<span style="color:#f5a524">●</span> фракционные')}
            ${check("passive", '<span style="color:#9be29b">●</span> мирные и свои')}
            ${check("playerinfo", "Подробно: статус, маунт, HP, оружие")}
            ${check("equipment", "Значки снаряжения (нужны значки из игры)")}
            ${text("myguild", "Моя гильдия")}
            ${text("myalliance", "Мой альянс")}
          </fieldset>
          <fieldset class="radar-opts"><legend>Прочее</legend>
            ${[RADAR_KINDS[1], RADAR_KINDS[3], RADAR_KINDS[4]].map(layer).join("")}
          </fieldset>
        </div>
      </div>
      <h2>Игроки рядом</h2>
      <div id="radar-players"></div>
      <h2>Ближайшие объекты</h2>
      <div class="radar-list" id="radar-list"></div>
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
      { key: "name", title: "Игрок", html: (r) => `<b>${esc(r.name)}</b>` },
      { key: "guild", title: "Гильдия", html: (r) => esc(r.guild || "—") },
      { key: "alliance", title: "Альянс", html: (r) => esc(r.alliance || "—") },
      { key: "status", title: "Статус", sort: (r) => r._st.key, html: (r) => `<span class="radar-dot" style="background:${r._st.color}"></span> ${esc(r._st.text)}` },
      { key: "hp", title: "HP", sort: (r) => (r.max_health ? r.health / r.max_health : -1),
        html: (r) => (r.max_health && r.health != null ? `${fmt(r.health)} / ${fmt(r.max_health)} (${Math.round(100 * r.health / r.max_health)}%)` : "—") },
      { key: "mounted", title: "Маунт", sort: (r) => (r.mounted ? 1 : 0), html: (r) => (r.mounted ? "♞ верхом" : r.mounted === false ? "пешком" : "—") },
      { key: "equipment", title: "Снаряжение", sort: (r) => (r.equipment || []).length,
        html: (r) => (r.equipment || []).filter((i) => i.slot !== "зелье" && i.slot !== "еда")
          .map((i) => `<span title="${esc(i.slot)}">${esc(i.name || i.id)}</span>`).join(", ") || "—" },
      { key: "dist", title: "Расст., м", html: (r) => fmt(r.dist) },
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
      .map((e) => ({ ...e, _st: playerStatus(e, me, o) })));
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
