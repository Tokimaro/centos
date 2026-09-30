"use strict";
// Радар: игроки, мобы, ресурсы и лут вокруг персонажа (события New…, Move, Leave, …Object)
// на фоне схемы текущей зоны. Отрисовка (RadarView) общая для вкладки и окна radar.html;
// настройки задаются на вкладке и хранятся в localStorage — окно подхватывает их сразу.

const RADAR_KINDS = [
  { kind: "player", title: "Игроки", color: "#e5484d" },
  { kind: "mob", title: "Мобы", color: "#f5a524" },
  { kind: "resource", title: "Ресурсы", color: "#30c47d" },
  { kind: "loot", title: "Лут и сундуки", color: "#4aa3ff" },
  { kind: "object", title: "Прочие объекты", color: "#b07cff" },
];
const RADAR_COLOR = Object.fromEntries(RADAR_KINDS.map((k) => [k.kind, k.color]));
const RADAR_KEY = "albion-trader-radar";
const RADAR_DEFAULTS = { player: true, mob: true, resource: true, loot: true, object: true,
  zoom: 4, rotate: true, labels: true, mintier: 1, background: true, bgopacity: 100, exits: true };

// Цвета схемы зоны (как у мини-карты игры — одинаковые в светлой и тёмной теме).
const ZONE_STYLE = {
  base: "#28332b", low: [44, 58, 42], high: [104, 120, 76],
  water: "#2d6f93", road: "#9a8759", cliff: "rgba(12,15,13,0.7)", building: "#6e5438",
  plot: "rgba(255,255,255,0.07)",
  rock: "#6f7470", tree: "#1d4a2a",
};
const ZONE_ORDER = ["ground", "water", "plot", "road", "cliff", "building", "rock", "tree"];

function radarOptions() { return { ...RADAR_DEFAULTS, ...loadSettings(RADAR_KEY) }; }

function radarLabel(e) {
  if (e.kind === "player") return e.name + (e.guild ? ` [${e.guild}]` : "");
  if (e.kind === "resource") {
    return `${e.name} T${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}${e.size != null ? " ×" + e.size : ""}`;
  }
  if (e.kind === "mob") return (e.name || `моб #${e.type_id ?? "?"}`) + (e.enchant ? ` .${e.enchant}` : "");
  return e.name || e.event;
}

function radarVisible(e, o) {
  if (!o[e.kind]) return false;
  return !(e.kind === "resource" && e.tier != null && e.tier < o.mintier);
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
    if (zone !== this.zone) { this.zone = zone; this.map = null; this.image = null; }
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

  draw() {
    const cv = this.canvas, ctx = cv.getContext("2d"), o = this.opts;
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    const me = this.data.me, scale = o.zoom, cx = w / 2, cy = h / 2;
    const rot = o.rotate ? -Math.PI / 4 : 0, cs = Math.cos(rot), sn = Math.sin(rot);
    const at = (x, y) => {
      const dx = x - me.x, dy = y - me.y;
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

    ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.lineWidth = 1;
    ctx.fillStyle = "rgba(255,255,255,0.45)"; ctx.font = "10px system-ui, sans-serif"; ctx.textAlign = "left";
    for (let r = 25; r * scale < Math.hypot(w, h) / 2; r += 25) {
      ctx.beginPath(); ctx.arc(cx, cy, r * scale, 0, 2 * Math.PI); ctx.stroke();
      if (!this.compact && r % 50 === 0) ctx.fillText(`${r} м`, cx + r * scale + 3, cy - 3);
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
        if (label && o.labels) { ctx.strokeText(label, sx, sy - 9); ctx.fillText(label, sx, sy - 9); }
      }
    }

    ctx.font = "11px system-ui, sans-serif";
    for (const e of this.data.entities.slice().reverse()) {
      if (!radarVisible(e, o)) continue;
      const [sx, sy] = at(e.x, e.y);
      if (sx < -30 || sy < -30 || sx > w + 30 || sy > h + 30) continue;
      ctx.fillStyle = RADAR_COLOR[e.kind]; ctx.strokeStyle = "rgba(0,0,0,0.7)"; ctx.lineWidth = 1.5;
      ctx.beginPath();
      if (e.kind === "player") ctx.arc(sx, sy, 5, 0, 2 * Math.PI);
      else if (e.kind === "resource") ctx.rect(sx - 3.5, sy - 3.5, 7, 7);
      else if (e.kind === "loot") { ctx.moveTo(sx, sy - 5); ctx.lineTo(sx + 5, sy); ctx.lineTo(sx, sy + 5); ctx.lineTo(sx - 5, sy); ctx.closePath(); }
      else ctx.arc(sx, sy, 3.5, 0, 2 * Math.PI);
      ctx.stroke(); ctx.fill();
      if (e.max_health && e.health != null) {
        ctx.fillStyle = "rgba(0,0,0,0.6)"; ctx.fillRect(sx - 10, sy + 7, 20, 3);
        ctx.fillStyle = "#4cc27d"; ctx.fillRect(sx - 10, sy + 7, 20 * Math.max(0, Math.min(1, e.health / e.max_health)), 3);
      }
      if (o.labels) {
        ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.75)"; ctx.fillStyle = "#f1f1f1";
        ctx.strokeText(radarLabel(e), sx, sy - 8); ctx.fillText(radarLabel(e), sx, sy - 8);
      }
    }
    ctx.fillStyle = "#ffffff"; ctx.strokeStyle = "#000"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, 5, 0, 2 * Math.PI); ctx.stroke(); ctx.fill();

    if (this.hud) {
      const n = {};
      for (const e of this.data.entities) n[e.kind] = (n[e.kind] || 0) + 1;
      const m = this.map;
      const bg = !this.zone ? "фон: зона неизвестна (смените зону)"
        : !m || m.status === "loading" ? "фон: скачиваю схему зоны…"
          : m.status === "error" ? `фон: ${m.error}` : "";
      this.hud.innerHTML = this.error ? `<span class="bad">Нет связи: ${esc(this.error)}</span>`
        : `<b>${esc(me.name || "персонаж")}</b> · ${esc(me.zone_name || "зона неизвестна")} · (${fmt1(me.x)}, ${fmt1(me.y)})<br>
          игроки ${n.player || 0} · мобы ${n.mob || 0} · ресурсы ${n.resource || 0} · лут ${n.loot || 0} · объекты ${n.object || 0}`
          + (bg ? `<br><span class="muted">${esc(bg)}</span>` : "");
    }
  }
}

if (typeof App !== "undefined" && document.getElementById("groups")) App.tab({
  id: "radar", group: "world", title: "Радар",
  init(el) {
    const o = radarOptions();
    const check = (k, t) => `<label><input type="checkbox" data-opt="${k}"${o[k] ? " checked" : ""}> ${t}</label>`;
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
          <fieldset class="radar-opts"><legend>Слои</legend>
            ${RADAR_KINDS.map((k) => `<label><input type="checkbox" data-opt="${k.kind}"${o[k.kind] ? " checked" : ""}>
              <span class="radar-dot" style="background:${k.color}"></span>${k.title}</label>`).join("")}
            ${check("exits", "Выходы из зоны")}
          </fieldset>
          <fieldset class="radar-opts"><legend>Вид</legend>
            ${check("background", "Фон — схема зоны")}
            <label>Яркость фона <input type="range" min="10" max="100" data-opt="bgopacity" value="${o.bgopacity}"></label>
            <label>Масштаб <input type="range" min="1" max="20" step="0.5" data-opt="zoom" value="${o.zoom}"></label>
            ${check("rotate", "Поворот 45° (как камера игры)")}
            ${check("labels", "Подписи")}
            <label>Мин. тир ресурсов <input type="number" min="1" max="8" data-opt="mintier" value="${o.mintier}"></label>
          </fieldset>
          <h2>Рядом</h2>
          <div class="radar-list" id="radar-list"></div>
        </div>
      </div>
      <details class="radar-codes"><summary>Коды событий (диагностика)</summary>
        <p class="muted">Все события, что пришли от сервера: код, имя (если известно), количество и параметры с типами.
          <code>·pos</code> — значение похоже на координаты. Если на радаре пусто, по этой таблице видно, под каким
          кодом приходят, например, мобы, — его можно указать в <code>data/opcodes.json</code>:
          <code>{"events": {"new_mob": 126}}</code>.</p>
        <div id="radar-codes"></div>
      </details>`;
    this.view = new RadarView($("#radar-canvas", el), { hud: $("#radar-hud", el) });
    $$("[data-opt]", el).forEach((inp) => inp.addEventListener("input", () => {
      const s = radarOptions();
      s[inp.dataset.opt] = inp.type === "checkbox" ? inp.checked : Number(inp.value);
      saveSettings(s, RADAR_KEY);
      this.view.opts = s;
      this.renderList();
    }));
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
    const frame = () => { if (App.current === this) this.view.draw(); requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
    setInterval(() => { if (App.current === this && !document.hidden) this.poll(); }, 300);
    setInterval(() => { if (App.current === this && !document.hidden) this.renderCodes(); }, 3000);
    this.poll();
  },
  show() { if (this.view) this.poll(); },

  async poll() {
    await this.view.poll();
    this.renderList();
  },

  renderList() {
    const o = this.view.opts;
    const rows = this.view.data.entities.filter((e) => radarVisible(e, o)).slice(0, 60);
    $("#radar-list").innerHTML = rows.length ? rows.map((e) =>
      `<div><span class="radar-dot" style="background:${RADAR_COLOR[e.kind]}"></span>
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
