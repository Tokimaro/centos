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
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function apiPost(path, body = {}) {
  const r = await fetch(new URL(path, location.origin), {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
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

// ---------- приложение и навигация ----------
const App = {
  groups: [
    { id: "trade", title: "Торговля" },
    { id: "prod", title: "Производство" },
    { id: "my", title: "Мои данные" },
    { id: "world", title: "Мир" },
    { id: "alerts", title: "Оповещения" },
    { id: "status", title: "Статус" },
  ],
  tabs: [],            // {id, group, title, init, show, el, ready}
  current: null,
  locations: [],
  locNames: {},
  defaultCities: [],

  tab(def) { this.tabs.push(def); },
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

  const read = () => {
    const out = {};
    for (const f of spec.flatMap((g) => g.fields)) {
      if (["markets", "tiers", "enchants", "qualities"].includes(f.type)) {
        out[f.name || f.type] = $$(`input[name=${f.name || f.type}]:checked`, form).map((i) => i.value);
      } else if (f.type === "check") {
        out[f.name] = form.elements[f.name].checked;
      } else if (f.name) {
        out[f.name] = form.elements[f.name].value;
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
