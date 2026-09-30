"use strict";
// Ядро интерфейса: утилиты, API, навигация по группам/вкладкам,
// генератор форм фильтров и сортируемые таблицы.

const QUALITY = { 1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр" };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------- утилиты ----------
const fmt = (n) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : Math.round(n).toLocaleString("ru-RU");
const fmt1 = (n) => (n === null || n === undefined) ? "—" : Number(n).toFixed(1);
const pct = (n) => (n === null || n === undefined) ? "—" : `${Number(n).toFixed(1)}%`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function age(ts, now) {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s} с`;
  if (s < 3600) return `${Math.floor(s / 60)} мин`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} ч`;
  return `${(s / 86400).toFixed(1)} д`;
}
function ageClass(ts, now) {
  if (!ts) return "";
  const s = now - ts;
  return s < 1800 ? "good" : s < 3 * 3600 ? "" : s < 12 * 3600 ? "warn" : "bad";
}
function dateTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v === "" || v === undefined || v === null) continue;
    url.searchParams.set(k, Array.isArray(v) ? v.join(",") : (v === true ? 1 : v === false ? 0 : v));
  }
  const r = await fetch(url);
  if (!r.ok) throw new Error(await apiError(r));
  return r.json();
}
async function apiError(r) {
  const text = await r.text();
  try { return JSON.parse(text).error || text; } catch { return `${r.status} ${text}`; }
}
async function apiPost(path, body = {}) {
  const r = await fetch(new URL(path, location.origin), {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await apiError(r));
  return r.json();
}

function loadSettings(key) {
  try { return JSON.parse(localStorage.getItem(key)) || {}; } catch { return {}; }
}
function saveSettings(s, key) {
  try { localStorage.setItem(key, JSON.stringify(s)); } catch { /* приватный режим */ }
}

function checkboxList(container, name, items, checked) {
  container.innerHTML = "";
  for (const it of items) {
    const l = document.createElement("label");
    l.innerHTML = `<input type="checkbox" name="${name}" value="${esc(it.value)}"${checked.includes(it.value) ? " checked" : ""}> <span class="${it.cls || ""}">${esc(it.label)}</span>`;
    container.appendChild(l);
  }
  const all = document.createElement("button");
  all.type = "button"; all.className = "quick"; all.textContent = "все / никого";
  all.addEventListener("click", () => {
    const boxes = $$(`input[name=${name}]`, container);
    const anyOff = boxes.some((b) => !b.checked);
    boxes.forEach((b) => { b.checked = anyOff; });
    container.dispatchEvent(new Event("change", { bubbles: true }));
  });
  container.appendChild(all);
}

// ---------- индикатор сборщика в шапке ----------
async function refreshConn() {
  try {
    const s = await api("/api/status");
    const c = s.capture;
    const el = $("#conn");
    const t = s.topics.find((x) => x.topic === "marketorders.ingest");
    const last = t ? `, данные ${age(t.last_at, s.now)} назад` : "";
    if (c.enabled && c.error) {
      el.textContent = `сборщик: ошибка — ${c.error}`; el.className = "conn bad";
    } else if (c.enabled && c.running) {
      const enc = c.encrypted_at && s.now - c.encrypted_at < 600;
      const loc = c.location_name ? ` · ${c.location_name}` : " · локация не определена — смените зону в игре, иначе цены не сохраняются";
      el.textContent = `сборщик работает${loc}${last}${enc ? " · данные рынка зашифрованы игрой" : ""}`;
      el.className = "conn " + (enc || !c.location_name ? "bad" : t && s.now - t.last_at < 600 ? "ok" : "old");
    } else {
      el.textContent = t ? `сборщик выключен${last}` : "сборщик выключен, данных нет";
      el.className = "conn old";
    }
  } catch { /* сервер недоступен */ }
}

// ---------- приложение и навигация ----------
const App = {
  groups: [
    { id: "trade", title: "Торговля" },
    { id: "prod", title: "Производство" },
    { id: "my", title: "Мои данные" },
    { id: "world", title: "Мир" },
    { id: "tools", title: "Инструменты" },
    { id: "alerts", title: "Оповещения" },
    { id: "status", title: "Статус" },
  ],
  tabs: [],            // {id, group, title, init, show, el, ready}
  current: null,
  locations: [],
  locNames: {},
  defaultCities: [],

  tab(def) { this.tabs.push(def); },
  // Живое обновление открытой вкладки (live + refresh) — для окна-компаньона
  // и вкладок инструментов; скрытая страница не опрашивает сервер.
  startAutoRefresh(ms = 20000) {
    clearInterval(this._timer);
    this._timer = setInterval(() => {
      const t = this.current;
      if (t && t.ready && t.live && t.refresh && !document.hidden) {
        try { t.refresh(t.el); } catch (e) { console.error(e); }
      }
    }, ms);
  },
  byId(id) { return this.tabs.find((t) => t.id === id); },
  locName(key) { return this.locNames[key] || key; },
  locClass(key) { return key === "black_market" ? "bm" : ""; },

  async start() {
    const data = await api("/api/locations");
    this.locations = data.locations;
    this.locNames = Object.fromEntries(data.locations.map((l) => [l.key, l.name]));
    this.defaultCities = data.default_cities;
    this.renderNav();
    window.addEventListener("hashchange", () => this.fromHash());
    this.fromHash();
  },

  renderNav() {
    const nav = $("#groups");
    nav.innerHTML = this.groups.filter((g) => this.tabs.some((t) => t.group === g.id))
      .map((g) => `<button class="tab" data-group="${g.id}">${esc(g.title)}</button>`).join("");
    $$("button", nav).forEach((b) => b.addEventListener("click", () => {
      const last = loadSettings("albion-trader-nav")[b.dataset.group];
      const first = this.tabs.find((t) => t.group === b.dataset.group);
      this.go(last && this.byId(last) ? last : first.id);
    }));
  },

  fromHash() {
    const id = location.hash.replace(/^#/, "").split("/").pop();
    this.show(this.byId(id) ? id : this.tabs[0].id);
  },

  go(id) {
    const t = this.byId(id);
    if (!t) return;
    const hash = `#${t.group}/${t.id}`;
    if (location.hash !== hash) location.hash = hash; else this.show(id);
  },

  show(id) {
    const t = this.byId(id);
    this.current = t;
    const nav = loadSettings("albion-trader-nav");
    nav[t.group] = t.id;
    saveSettings(nav, "albion-trader-nav");
    $$("#groups .tab").forEach((b) => b.classList.toggle("active", b.dataset.group === t.group));
    const siblings = this.tabs.filter((x) => x.group === t.group);
    const sub = $("#subnav");
    sub.hidden = siblings.length < 2;
    sub.innerHTML = siblings.map((x) => `<button class="subtab${x.id === t.id ? " active" : ""}" data-id="${x.id}">${esc(x.title)}</button>`).join("");
    $$("button", sub).forEach((b) => b.addEventListener("click", () => this.go(b.dataset.id)));
    for (const x of this.tabs) {
      if (!x.el) x.el = this.panel(x);
      x.el.classList.toggle("active", x === t);
    }
    if (!t.ready) {
      t.ready = true;
      try { t.init && t.init(t.el); } catch (e) { console.error(e); }
    }
    t.show && t.show(t.el);
  },

  panel(t) {
    let el = document.getElementById(`tab-${t.id}`);
    if (!el) {
      el = document.createElement("section");
      el.id = `tab-${t.id}`;
      el.className = "panel";
      $("main").appendChild(el);
    }
    return el;
  },

  openItem(itemId) {
    const f = $("#price-form");
    if (!f) return;
    f.item.value = itemId;
    this.go("prices");
    if (typeof loadPrices === "function") loadPrices();
  },
};

// ---------- генератор форм фильтров ----------
// spec: [{legend, fields: [...]}]; поля:
//   {type: "markets", name, label, buyable?, default?}  — чекбоксы рынков
//   {type: "market", name, label, empty?}              — выпадающий список рынков
//   {type: "number"|"search"|"text", name, label, value, step, min, placeholder}
//   {type: "select", name, label, options: [[value, text]], value}
//   {type: "check", name, label, value}
//   {type: "tiers"} / {type: "enchants"}
//   {type: "qualities"}
function buildForm(container, storeKey, spec, { submitText = "Показать", onSubmit, intro } = {}) {
  const saved = loadSettings(storeKey);
  const form = document.createElement("form");
  form.className = "filters";
  form.autocomplete = "off";
  const lists = [];
  for (const group of spec) {
    const fs = document.createElement("fieldset");
    fs.className = group.cls || "opts";
    fs.innerHTML = `<legend>${esc(group.legend)}</legend>`;
    for (const f of group.fields) fs.appendChild(buildField(f, saved, lists));
    form.appendChild(fs);
  }
  const last = form.lastElementChild;
  const btn = document.createElement("button");
  btn.type = "submit"; btn.className = "primary"; btn.textContent = submitText;
  last.appendChild(btn);
  if (intro) container.insertAdjacentHTML("beforeend", `<p class="muted intro">${intro}</p>`);
  container.appendChild(form);

  // По атрибуту name, а не form.elements[name]: у коллекции есть свои методы (item, length…).
  const field = (name) => form.querySelector(`[name="${name}"]`);
  const read = () => {
    const out = {};
    for (const f of spec.flatMap((g) => g.fields)) {
      if (["markets", "tiers", "enchants", "qualities"].includes(f.type)) {
        out[f.name || f.type] = $$(`input[name=${f.name || f.type}]:checked`, form).map((i) => i.value);
      } else if (f.type === "check") {
        out[f.name] = field(f.name).checked;
      } else if (f.name) {
        out[f.name] = field(f.name).value;
      }
    }
    return out;
  };
  form.addEventListener("change", () => saveSettings(read(), storeKey));
  form.addEventListener("submit", (e) => { e.preventDefault(); saveSettings(read(), storeKey); onSubmit && onSubmit(read()); });
  return { form, read };
}

function buildField(f, saved, lists) {
  const wrap = document.createElement("div");
  const val = saved[f.name || f.type] !== undefined ? saved[f.name || f.type] : f.value;
  if (f.type === "markets") {
    const markets = App.locations.filter((l) => !(f.buyable && l.kind === "black_market"));
    const def = f.default || (f.buyable ? App.defaultCities : [...App.defaultCities, "black_market"]);
    wrap.className = "checks";
    checkboxList(wrap, f.name, markets.map((l) => ({ value: l.key, label: l.name, cls: App.locClass(l.key) })),
      Array.isArray(val) ? val : def);
    if (f.label) { const d = document.createElement("div"); d.innerHTML = `<div class="field-label">${esc(f.label)}</div>`; d.appendChild(wrap); return d; }
    return wrap;
  }
  if (f.type === "tiers" || f.type === "enchants" || f.type === "qualities") {
    const items = f.type === "tiers" ? [2, 3, 4, 5, 6, 7, 8].map((t) => ({ value: String(t), label: `T${t}` }))
      : f.type === "enchants" ? [0, 1, 2, 3, 4].map((e) => ({ value: String(e), label: `.${e}` }))
        : [1, 2, 3, 4, 5].map((q) => ({ value: String(q), label: QUALITY[q] }));
    wrap.className = "checks small";
    checkboxList(wrap, f.name || f.type, items, Array.isArray(val) ? val : (f.value || []));
    return wrap;
  }
  if (f.type === "check") {
    wrap.innerHTML = `<label class="inline"><input type="checkbox" name="${f.name}"${val ? " checked" : ""}> ${esc(f.label)}</label>`;
    return wrap.firstElementChild;
  }
  const label = document.createElement("label");
  label.textContent = f.label || "";
  if (f.title) label.title = f.title;
  let input;
  if (f.type === "select" || f.type === "market") {
    input = document.createElement("select");
    const options = f.type === "market"
      ? [...(f.empty ? [["", f.empty]] : []), ...App.locations.filter((l) => !(f.buyable && l.kind === "black_market")).map((l) => [l.key, l.name])]
      : f.options;
    input.innerHTML = options.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
    if (val !== undefined) input.value = val;
  } else {
    input = document.createElement("input");
    input.type = f.type === "number" ? "number" : f.type === "search" ? "search" : "text";
    if (f.type === "number") { input.step = f.step || "any"; if (f.min !== undefined) input.min = f.min; }
    if (f.placeholder) input.placeholder = f.placeholder;
    if (val !== undefined) input.value = val;
  }
  input.name = f.name;
  label.appendChild(input);
  return label;
}

// ---------- сортируемая таблица ----------
// columns: [{key, title, num?, html?(row), sort?(row), cls?(row), hint?}]
function makeTable(container, columns, { sort, asc = false, empty = "Нет данных", rowClass } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  wrap.appendChild(table);
  container.appendChild(wrap);
  let rows = [];
  let key = sort || null;
  let ascending = asc;
  const render = () => {
    table.innerHTML = `<thead><tr>${columns.map((c) =>
      `<th data-key="${c.key}" class="${c.num ? "num " : ""}${c.key === key ? "sorted" + (ascending ? " asc" : "") : ""}"${c.hint ? ` title="${esc(c.hint)}"` : ""}>${esc(c.title)}</th>`).join("")}</tr></thead>`;
    const col = columns.find((c) => c.key === key);
    const val = (r) => {
      const v = col && col.sort ? col.sort(r) : r[key];
      return v === null || v === undefined ? (ascending ? Infinity : -Infinity) : v;
    };
    const sorted = col ? [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      const c = typeof x === "string" && typeof y === "string" ? x.localeCompare(y, "ru") : (x > y) - (x < y);
      return ascending ? c : -c;
    }) : rows;
    const body = sorted.length ? sorted.map((r) => `<tr class="${rowClass ? rowClass(r) : ""}">${columns.map((c) =>
      `<td class="${c.num ? "num " : ""}${c.cls ? c.cls(r) : ""}">${c.html ? c.html(r) : esc(r[c.key] ?? "—")}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${columns.length}" class="muted">${empty}</td></tr>`;
    table.insertAdjacentHTML("beforeend", `<tbody>${body}</tbody>`);
    $$("th", table).forEach((th) => th.addEventListener("click", () => {
      if (key === th.dataset.key) ascending = !ascending; else { key = th.dataset.key; ascending = false; }
      render();
    }));
    $$(".item-name", table).forEach((el) => el.addEventListener("click", () => App.openItem(el.dataset.item)));
  };
  render();
  return { set(r) { rows = r || []; render(); }, table, get rows() { return rows; } };
}

function itemCell(r, idKey = "item_id", nameKey = "name") {
  return `<span class="item-name" data-item="${esc(r[idKey])}">${esc(r[nameKey] || r[idKey])}</span><br><span class="item-id">${esc(r[idKey])}</span>`;
}
function locCell(key) { return `<span class="${App.locClass(key)}">${esc(App.locName(key))}</span>`; }

function summaryLine(el, text, error) {
  el.innerHTML = error ? `<span class="bad">Ошибка: ${esc(error)}</span>` : text;
}

// Раздел со стандартной раскладкой: вступление, форма, сводка, таблица.
function standardTab({ id, group, title, intro, storeKey, spec, columns, sort, asc, empty, load, submitText, extra }) {
  App.tab({
    id, group, title,
    init(el) {
      const ctx = {};
      ctx.form = buildForm(el, storeKey || `albion-trader-${id}`, spec, {
        intro, submitText, onSubmit: () => ctx.reload(),
      });
      if (extra) extra(el, ctx);
      ctx.summary = document.createElement("div");
      ctx.summary.className = "summary";
      el.appendChild(ctx.summary);
      ctx.table = makeTable(el, columns, { sort, asc, empty });
      ctx.reload = async () => {
        ctx.summary.textContent = "Загрузка…";
        try {
          const res = await load(ctx.form.read(), ctx);
          ctx.table.set(res.rows);
          ctx.summary.innerHTML = res.summary || "";
        } catch (e) { summaryLine(ctx.summary, "", e.message); }
      };
      this.ctx = ctx;
      ctx.reload();
    },
  });
}

// ---------- линейный график (SVG, без библиотек) ----------
// series: [{name, slot: 1..3, points: [[ts, value], ...]}]; одна ось Y.
function lineChart(container, series, { height = 260, yFormat = fmt, empty = "Нет данных для графика" } = {}) {
  container.innerHTML = "";
  container.classList.add("viz");
  const all = series.flatMap((s) => s.points.filter((p) => p[1] !== null && p[1] !== undefined));
  if (!all.length) { container.innerHTML = `<p class="muted">${esc(empty)}</p>`; return; }
  const legend = document.createElement("div");
  legend.className = "viz-legend";
  for (const s of series) {
    const item = document.createElement("span");
    item.innerHTML = `<svg width="18" height="8" aria-hidden="true"><line x1="1" y1="4" x2="17" y2="4" stroke="var(--series-${s.slot})" stroke-width="2" stroke-linecap="round"/></svg>`;
    item.appendChild(document.createTextNode(s.name));
    legend.appendChild(item);
  }
  container.appendChild(legend);

  const width = Math.max(320, container.clientWidth || 600);
  const m = { l: 64, r: 110, t: 10, b: 28 };
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  if (x0 === x1) { x0 -= 3600; x1 += 3600; }
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.1 || Math.abs(y1) * 0.1 || 1;
  y0 = Math.max(0, y0 - pad); y1 += pad;
  const X = (t) => m.l + (t - x0) / (x1 - x0) * (width - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - y0) / (y1 - y0)) * (height - m.t - m.b);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", width); svg.setAttribute("height", height);
  svg.setAttribute("role", "img");
  let html = "";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4, y = Y(v);
    html += `<line class="viz-grid" x1="${m.l}" x2="${width - m.r}" y1="${y}" y2="${y}"/>`
      + `<text class="viz-axis" x="${m.l - 6}" y="${y + 4}" text-anchor="end">${esc(yFormat(v))}</text>`;
  }
  const spanDays = (x1 - x0) / 86400;
  for (let i = 0; i <= 4; i++) {
    const t = x0 + (x1 - x0) * i / 4, x = X(t);
    const d = new Date(t * 1000);
    const label = spanDays > 2 ? d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })
      : d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    html += `<text class="viz-axis" x="${x}" y="${height - 8}" text-anchor="middle">${label}</text>`;
  }
  const ends = [];
  for (const s of series) {
    const pts = s.points.filter((p) => p[1] !== null && p[1] !== undefined).sort((a, b) => a[0] - b[0]);
    if (!pts.length) continue;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    html += `<path d="${d}" fill="none" stroke="var(--series-${s.slot})" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    if (pts.length === 1) html += `<circle cx="${X(pts[0][0])}" cy="${Y(pts[0][1])}" r="4" fill="var(--series-${s.slot})" stroke="var(--panel)" stroke-width="2"/>`;
    const last = pts[pts.length - 1];
    ends.push({ s, y: Y(last[1]), x: X(last[0]), v: last[1] });
  }
  // Подписи у концов линий; при наложении раздвигаем с выносками.
  ends.sort((a, b) => a.y - b.y);
  let prev = -Infinity;
  for (const e of ends) {
    const ly = Math.max(e.y, prev + 14);
    prev = ly;
    const lx = width - m.r + 8;
    html += `<line class="viz-leader" x1="${e.x}" y1="${e.y}" x2="${lx - 2}" y2="${ly}"/>`
      + `<text class="viz-label" x="${lx}" y="${ly + 4}">${esc(yFormat(e.v))}</text>`;
  }
  html += `<line class="viz-cross" x1="0" x2="0" y1="${m.t}" y2="${height - m.b}" visibility="hidden"/>`;
  svg.innerHTML = html;
  const box = document.createElement("div");
  box.className = "viz-box";
  box.appendChild(svg);
  const tip = document.createElement("div");
  tip.className = "viz-tip";
  tip.hidden = true;
  box.appendChild(tip);
  container.appendChild(box);

  const cross = svg.querySelector(".viz-cross");
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    if (px < m.l || px > width - m.r) { cross.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
    const t = x0 + (px - m.l) / (width - m.l - m.r) * (x1 - x0);
    const nearestT = xs.reduce((a, b) => (Math.abs(b - t) < Math.abs(a - t) ? b : a));
    const x = X(nearestT);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    tip.innerHTML = "";
    const head = document.createElement("div");
    head.className = "viz-tip-head";
    head.textContent = dateTime(nearestT);
    tip.appendChild(head);
    for (const s of series) {
      const pts = s.points.filter((p) => p[1] !== null && p[1] !== undefined && p[0] <= nearestT);
      if (!pts.length) continue;
      const p = pts[pts.length - 1];
      const row = document.createElement("div");
      row.innerHTML = `<svg width="14" height="8" aria-hidden="true"><line x1="1" y1="4" x2="13" y2="4" stroke="var(--series-${s.slot})" stroke-width="2"/></svg>`;
      const b = document.createElement("b"); b.textContent = yFormat(p[1]);
      const n = document.createElement("span"); n.className = "muted"; n.textContent = " " + s.name;
      row.append(b, n);
      tip.appendChild(row);
    }
    tip.hidden = false;
    tip.style.left = `${Math.min(x + 12, width - 180)}px`;
    tip.style.top = `${m.t}px`;
  };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

// ---------- экспорт таблиц в CSV ----------
// Разделитель «;», BOM и десятичная запятая — так файл сразу открывается в русском Excel.
function tableToCsv(table) {
  const clean = (td) => {
    let text = (td.innerText || td.textContent || "").trim().replace(/\s*\n\s*/g, " / ");
    if (td.classList && td.classList.contains("num")) {
      const compact = text.replace(/[\s  ]/g, "");
      if (/^[−-]?\d+(\.\d+)?%?$/.test(compact)) text = compact.replace("−", "-").replace(".", ",");
    }
    // Защита от формул в Excel: текст, начинающийся с = + - @, выводим как текст.
    if (/^[=+\-@\t\r]/.test(text) && !(td.classList && td.classList.contains("num") && /^-?\d/.test(text))) text = `'${text}`;
    return /[";\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const rows = [...table.querySelectorAll("tr")].filter((tr) => !tr.querySelector("td[colspan]"));
  return "﻿" + rows.map((tr) => [...tr.children].map(clean).join(";")).join("\r\n");
}

function downloadCsv(table, name) {
  const blob = new Blob([tableToCsv(table)], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${name}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function addCsvButtons(root = document) {
  for (const wrap of root.querySelectorAll(".table-wrap")) {
    if (wrap.dataset.csv) continue;
    wrap.dataset.csv = "1";
    const bar = document.createElement("div");
    bar.className = "table-tools";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "csv-btn";
    btn.textContent = "CSV";
    btn.title = "Скачать таблицу для Excel";
    btn.addEventListener("click", () => {
      const table = wrap.querySelector("table");
      const panel = wrap.closest(".panel");
      if (table) downloadCsv(table, panel ? panel.id.replace(/^tab-/, "") : "table");
    });
    bar.appendChild(btn);
    wrap.parentNode.insertBefore(bar, wrap);
  }
}
new MutationObserver(() => addCsvButtons()).observe(document.documentElement, { childList: true, subtree: true });
