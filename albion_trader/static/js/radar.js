"use strict";
// Радар: игроки, мобы, ресурсы и лут вокруг персонажа (события New…, Move, Leave, …Object).

const RADAR_KINDS = [
  { kind: "player", title: "Игроки", color: "#e5484d" },
  { kind: "mob", title: "Мобы", color: "#f5a524" },
  { kind: "resource", title: "Ресурсы", color: "#30a46c" },
  { kind: "loot", title: "Лут и сундуки", color: "#3e8ed0" },
  { kind: "object", title: "Прочие объекты", color: "#8e4ec6" },
];
const RADAR_COLOR = Object.fromEntries(RADAR_KINDS.map((k) => [k.kind, k.color]));

function radarLabel(e) {
  if (e.kind === "player") return e.name + (e.guild ? ` [${e.guild}]` : "");
  if (e.kind === "resource") {
    return `${e.name} T${e.tier ?? "?"}${e.enchant ? "." + e.enchant : ""}${e.size != null ? " ×" + e.size : ""}`;
  }
  if (e.kind === "mob") return (e.name || `моб #${e.type_id ?? "?"}`) + (e.enchant ? ` .${e.enchant}` : "");
  return e.name || e.event;
}

App.tab({
  id: "radar", group: "world", title: "Радар",
  init(el) {
    const opts = { player: true, mob: true, resource: true, loot: true, object: true,
      zoom: 8, rotate: true, labels: true, mintier: 1, ...loadSettings("albion-trader-radar") };
    this.opts = opts;
    el.innerHTML = `
      <p class="muted intro">Объекты вокруг персонажа из трафика игры: новые игроки, мобы, ресурсы, сундуки и прочие
        объекты, их движение и уход из зоны. Пока вкладка открыта, сборщик разбирает и события движения.
        Номера событий обновляются кнопкой во вкладке «Статус» (как у рынка); ключи параметров можно переопределить
        файлом <code>data/radar.json</code>.</p>
      <div class="radar">
        <div class="radar-map"><canvas id="radar-canvas"></canvas><div class="radar-hud" id="radar-hud"></div></div>
        <div class="radar-side">
          <fieldset class="radar-opts"><legend>Слои</legend>
            ${RADAR_KINDS.map((k) => `<label><input type="checkbox" data-opt="${k.kind}"${opts[k.kind] ? " checked" : ""}>
              <span class="radar-dot" style="background:${k.color}"></span>${k.title}</label>`).join("")}
          </fieldset>
          <fieldset class="radar-opts"><legend>Вид</legend>
            <label>Масштаб, пикс./м <input type="range" min="2" max="30" data-opt="zoom" value="${opts.zoom}"></label>
            <label><input type="checkbox" data-opt="rotate"${opts.rotate ? " checked" : ""}> Поворот 45° (как камера игры)</label>
            <label><input type="checkbox" data-opt="labels"${opts.labels ? " checked" : ""}> Подписи</label>
            <label>Мин. тир ресурсов <input type="number" min="1" max="8" data-opt="mintier" value="${opts.mintier}"></label>
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
    $$("[data-opt]", el).forEach((inp) => inp.addEventListener("input", () => {
      opts[inp.dataset.opt] = inp.type === "checkbox" ? inp.checked : Number(inp.value);
      saveSettings(opts, "albion-trader-radar");
      this.renderList();
    }));
    this.canvas = $("#radar-canvas", el);
    this.data = { me: { x: 0, y: 0 }, entities: [], codes: [] };
    const frame = () => { if (App.current === this) this.draw(); requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
    setInterval(() => { if (App.current === this && !document.hidden) this.poll(); }, 300);
    setInterval(() => { if (App.current === this && !document.hidden) this.renderCodes(); }, 3000);
    this.poll();
  },
  show() { this.poll(); },

  visible(e) {
    const o = this.opts;
    if (!o[e.kind]) return false;
    return !(e.kind === "resource" && e.tier != null && e.tier < o.mintier);
  },

  async poll() {
    try {
      this.data = await api("/api/radar");
      this.error = null;
      this.renderList();
    } catch (e) { this.error = e.message; }
  },

  renderList() {
    const rows = this.data.entities.filter((e) => this.visible(e)).slice(0, 60);
    $("#radar-list").innerHTML = rows.length ? rows.map((e) =>
      `<div><span class="radar-dot" style="background:${RADAR_COLOR[e.kind]}"></span>
        <span>${esc(radarLabel(e))}</span><span class="muted">${fmt(e.dist)} м</span></div>`).join("")
      : '<p class="muted">Никого. Смените зону, чтобы сборщик увидел объекты вокруг.</p>';
  },

  renderCodes() {
    const codes = this.data.codes || [];
    $("#radar-codes").innerHTML = codes.length ? `<div class="table-wrap"><table><thead><tr><th>Код</th><th>Имя</th>
      <th>Кол-во</th><th>Параметры</th></tr></thead><tbody>${codes.map((c) => `<tr><td>${c.code}</td>
      <td>${esc(c.name || "—")}</td><td>${fmt(c.count)}</td><td><code>${esc(c.shape)}</code></td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">Событий пока не было.</p>';
  },

  draw() {
    const cv = this.canvas, ctx = cv.getContext("2d"), o = this.opts;
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    const css = getComputedStyle(document.documentElement);
    const col = (v) => css.getPropertyValue(v).trim();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const me = this.data.me, scale = o.zoom, cx = w / 2, cy = h / 2;
    const rot = o.rotate ? -Math.PI / 4 : 0, cs = Math.cos(rot), sn = Math.sin(rot);
    const at = (x, y) => {
      const dx = x - me.x, dy = y - me.y;
      return [cx + (dx * cs - dy * sn) * scale, cy - (dx * sn + dy * cs) * scale];
    };
    ctx.strokeStyle = col("--viz-grid"); ctx.lineWidth = 1;
    ctx.fillStyle = col("--muted"); ctx.font = "10px system-ui, sans-serif"; ctx.textAlign = "left";
    for (let r = 10; r * scale < Math.hypot(w, h) / 2; r += 10) {
      ctx.beginPath(); ctx.arc(cx, cy, r * scale, 0, 2 * Math.PI); ctx.stroke();
      if (r % 20 === 0) ctx.fillText(`${r} м`, cx + r * scale + 3, cy - 3);
    }
    ctx.textAlign = "center"; ctx.font = "11px system-ui, sans-serif";
    for (const e of this.data.entities.slice().reverse()) {
      if (!this.visible(e)) continue;
      const [sx, sy] = at(e.x, e.y);
      if (sx < -30 || sy < -30 || sx > w + 30 || sy > h + 30) continue;
      ctx.fillStyle = RADAR_COLOR[e.kind];
      ctx.beginPath();
      if (e.kind === "player") ctx.arc(sx, sy, 5, 0, 2 * Math.PI);
      else if (e.kind === "resource") ctx.rect(sx - 3.5, sy - 3.5, 7, 7);
      else if (e.kind === "loot") { ctx.moveTo(sx, sy - 5); ctx.lineTo(sx + 5, sy); ctx.lineTo(sx, sy + 5); ctx.lineTo(sx - 5, sy); }
      else ctx.arc(sx, sy, 3.5, 0, 2 * Math.PI);
      ctx.fill();
      if (e.max_health && e.health != null) {
        ctx.fillStyle = col("--border"); ctx.fillRect(sx - 10, sy + 7, 20, 3);
        ctx.fillStyle = col("--good"); ctx.fillRect(sx - 10, sy + 7, 20 * Math.max(0, Math.min(1, e.health / e.max_health)), 3);
      }
      if (o.labels) { ctx.fillStyle = col("--text"); ctx.fillText(radarLabel(e), sx, sy - 8); }
    }
    ctx.fillStyle = col("--accent");
    ctx.beginPath(); ctx.arc(cx, cy, 5, 0, 2 * Math.PI); ctx.fill();

    const n = {};
    for (const e of this.data.entities) n[e.kind] = (n[e.kind] || 0) + 1;
    $("#radar-hud").innerHTML = this.error ? `<span class="bad">Нет связи: ${esc(this.error)}</span>`
      : `<b>${esc(me.name || "персонаж")}</b> · ${esc(me.zone_name || "зона неизвестна")} · (${fmt1(me.x)}, ${fmt1(me.y)})<br>
        игроки ${n.player || 0} · мобы ${n.mob || 0} · ресурсы ${n.resource || 0} · лут ${n.loot || 0} · объекты ${n.object || 0}`;
  },
});
