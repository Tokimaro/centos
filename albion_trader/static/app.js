"use strict";

const QUALITY = { 1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр" };
const STORE_KEY = "albion-trader-settings";
const FS_STORE_KEY = "albion-trader-fastsell";
const $ = (sel, root = document) => root.querySelector(sel);

let locations = [];
let locNames = {};
let lastDeals = [];
let sortKey = "total_profit";
let sortAsc = false;
let autoTimer = null;

// ---------- утилиты ----------
const fmt = (n) => (n === null || n === undefined) ? "—" : Math.round(n).toLocaleString("ru-RU");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function age(ts, now) {
  if (!ts) return "—";
  const s = Math.max(0, now - ts);
  if (s < 60) return `${s} с`;
  if (s < 3600) return `${Math.floor(s / 60)} мин`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} ч`;
  return `${(s / 86400).toFixed(1)} д`;
}
function ageClass(ts, now) {
  const s = now - ts;
  return s < 1800 ? "good" : s < 3 * 3600 ? "" : s < 12 * 3600 ? "warn" : "bad";
}
async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== "" && v !== undefined && v !== null) url.searchParams.set(k, v);
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function loadSettings(key = STORE_KEY) {
  try { return JSON.parse(localStorage.getItem(key)) || {}; } catch { return {}; }
}
function saveSettings(s, key = STORE_KEY) {
  try { localStorage.setItem(key, JSON.stringify(s)); } catch { /* приватный режим */ }
}

// ---------- вкладки ----------
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "status") loadStatus();
  if (name === "fastsell" && !fastSellLoaded) loadFastSell();
}
let fastSellLoaded = false;

// ---------- фильтры ----------
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
    const boxes = container.querySelectorAll(`input[name=${name}]`);
    const anyOff = [...boxes].some((b) => !b.checked);
    boxes.forEach((b) => { b.checked = anyOff; });
  });
  container.appendChild(all);
}

function readFilters() {
  const f = $("#filters");
  const vals = (name) => [...f.querySelectorAll(`input[name=${name}]:checked`)].map((i) => i.value);
  return {
    src: vals("src"), dst: vals("dst"), tiers: vals("tier"), enchants: vals("enchant"),
    buy: f.buy.value, sell: f.sell.value, premium: f.premium.checked,
    max_age: f.max_age.value, min_profit: f.min_profit.value, min_margin: f.min_margin.value,
    min_total: f.min_total.value, q: f.q.value, auto: f.auto.checked,
  };
}

function applySettings(s) {
  const f = $("#filters");
  for (const k of ["buy", "sell", "max_age", "min_profit", "min_margin", "min_total", "q"]) if (s[k] !== undefined) f[k].value = s[k];
  if (s.premium !== undefined) f.premium.checked = s.premium;
  if (s.auto !== undefined) f.auto.checked = s.auto;
}

async function init() {
  const s = loadSettings();
  const data = await api("/api/locations");
  locations = data.locations;
  locNames = Object.fromEntries(locations.map((l) => [l.key, l.name]));
  const buyable = locations.filter((l) => l.kind !== "black_market");
  const cities = data.default_cities;
  checkboxList($("#src-list"), "src", buyable.map((l) => ({ value: l.key, label: l.name })), s.src || cities);
  checkboxList($("#dst-list"), "dst",
    locations.map((l) => ({ value: l.key, label: l.name, cls: l.kind === "black_market" ? "bm" : "" })),
    s.dst || [...cities, "black_market"]);
  checkboxList($("#tiers"), "tier", [2, 3, 4, 5, 6, 7, 8].map((t) => ({ value: String(t), label: `T${t}` })), s.tiers || []);
  checkboxList($("#enchants"), "enchant", [0, 1, 2, 3, 4].map((e) => ({ value: String(e), label: `.${e}` })), s.enchants || []);
  applySettings(s);
  initFastSell(data);

  $("#preset-fastsell").addEventListener("click", () => {
    const f = $("#filters");
    f.buy.value = "order";
    f.sell.value = "instant";
    loadDeals();
  });
  $("#filters").addEventListener("submit", (e) => { e.preventDefault(); loadDeals(); });
  $("#filters").addEventListener("change", () => { saveSettings(readFilters()); scheduleAuto(); });
  $("#price-form").addEventListener("submit", (e) => { e.preventDefault(); loadPrices(); });
  $("#price-form").item.addEventListener("input", debounce(suggestItems, 250));
  document.querySelectorAll("#deals th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = ["name", "source", "age"].includes(k); }
    renderDeals();
  }));
  loadDeals();
  refreshConn();
  setInterval(refreshConn, 15000);
  scheduleAuto();
}

function scheduleAuto() {
  clearInterval(autoTimer);
  if ($("#filters").auto.checked) autoTimer = setInterval(loadDeals, 30000);
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------- сделки ----------
async function loadDeals() {
  const f = readFilters();
  saveSettings(f);
  const summary = $("#deals-summary");
  summary.textContent = "Загрузка…";
  try {
    const data = await api("/api/deals", {
      src: f.src.join(","), dst: f.dst.join(","), tiers: f.tiers.join(","), enchants: f.enchants.join(","),
      buy: f.buy, sell: f.sell, premium: f.premium ? 1 : 0, max_age: f.max_age,
      min_profit: f.min_profit, min_margin: f.min_margin, min_total: f.min_total, q: f.q, limit: 500,
    });
    lastDeals = data.deals.map((d) => ({ ...d, age: data.now - Math.min(d.buy_seen_at, d.sell_seen_at), _now: data.now }));
    const shown = data.deals.length < data.count ? ` (показано ${data.deals.length})` : "";
    summary.textContent = `Найдено сделок: ${data.count}${shown}. Налог ${(data.tax * 100).toFixed(1)}%, комиссия за размещение ${(data.setup_fee * 100).toFixed(1)}%. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.`;
    renderDeals();
  } catch (e) {
    summary.innerHTML = `<span class="bad">Ошибка: ${esc(e.message)}</span>`;
  }
}

function renderDeals() {
  document.querySelectorAll("#deals th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === sortKey);
    th.classList.toggle("asc", th.dataset.sort === sortKey && sortAsc);
  });
  const val = (d) => {
    const v = d[sortKey];
    if (sortKey === "source") return `${locNames[d.source] || d.source} ${locNames[d.destination] || d.destination}`;
    return v === null || v === undefined ? (sortAsc ? Infinity : -Infinity) : v;
  };
  const rows = [...lastDeals].sort((a, b) => {
    const x = val(a), y = val(b);
    const c = typeof x === "string" ? x.localeCompare(y, "ru") : x - y;
    return sortAsc ? c : -c;
  });
  const tbody = $("#deals tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="12" class="muted">Сделок нет. Откройте в игре рынки нескольких городов (и Чёрный рынок), чтобы клиент собрал цены, или ослабьте фильтры.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((d) => {
    const q = d.buy_quality !== d.sell_quality ? `${d.sell_quality} <span class="muted" title="Покупать качество ${d.buy_quality}">(${d.buy_quality})</span>` : d.sell_quality;
    const dstCls = d.destination === "black_market" ? "bm" : "";
    const oldest = Math.min(d.buy_seen_at, d.sell_seen_at);
    return `<tr>
      <td><span class="item-name" data-item="${esc(d.item_id)}">${esc(d.name)}</span><br><span class="item-id">${esc(d.item_id)}</span></td>
      <td title="${QUALITY[d.sell_quality] || ""}">${q}</td>
      <td>${esc(locNames[d.source] || d.source)} → <span class="${dstCls}">${esc(locNames[d.destination] || d.destination)}</span></td>
      <td class="num">${fmt(d.buy_price)}</td>
      <td class="num">${fmt(d.sell_price)}</td>
      <td class="num good">${fmt(d.unit_profit)}</td>
      <td class="num">${d.margin.toFixed(1)}%</td>
      <td class="num">${d.units === null ? "∞" : fmt(d.units)}</td>
      <td class="num good"><b>${fmt(d.total_profit)}</b></td>
      <td class="num">${fmt(d.investment)}</td>
      <td class="num">${d.daily_volume === null || d.daily_volume === undefined ? "—" : fmt(d.daily_volume)}</td>
      <td class="num ${ageClass(oldest, d._now)}" title="покупка: ${age(d.buy_seen_at, d._now)}, продажа: ${age(d.sell_seen_at, d._now)}">${age(oldest, d._now)}</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll(".item-name").forEach((el) => el.addEventListener("click", () => {
    $("#price-form").item.value = el.dataset.item;
    showTab("prices");
    loadPrices();
  }));
}

// ---------- цены предмета ----------
async function suggestItems() {
  const q = $("#price-form").item.value.trim();
  if (q.length < 2) return;
  const data = await api("/api/items", { q, limit: 30 });
  $("#item-options").innerHTML = data.items.map((i) => `<option value="${esc(i.item_id)}">${esc(i.name)}</option>`).join("");
}

async function loadPrices() {
  const f = $("#price-form");
  let item = f.item.value.trim();
  if (!item) return;
  if (!/^[A-Z0-9_@]+$/.test(item)) {
    const found = await api("/api/items", { q: item, limit: 1 });
    if (found.items.length) { item = found.items[0].item_id; f.item.value = item; }
  }
  const data = await api("/api/prices", { item, max_age: f.max_age.value });
  $("#price-title").textContent = `${data.name} (${data.item_id})`;
  const sells = data.rows.filter((r) => r.sell_min !== null && r.location !== "black_market").map((r) => r.sell_min);
  const buys = data.rows.filter((r) => r.buy_max !== null).map((r) => r.buy_max);
  const minSell = sells.length ? Math.min(...sells) : null;
  const maxBuy = buys.length ? Math.max(...buys) : null;
  const tbody = $("#prices tbody");
  tbody.innerHTML = data.rows.length ? data.rows.map((r) => `<tr>
      <td class="${r.location === "black_market" ? "bm" : ""}">${esc(r.name)}</td>
      <td>${QUALITY[r.quality] || r.quality}</td>
      <td class="num ${r.sell_min !== null && r.sell_min === minSell ? "good" : ""}">${fmt(r.sell_min)}</td>
      <td class="num">${r.sell_amount || "—"}</td>
      <td class="num ${r.sell_seen_at ? ageClass(r.sell_seen_at, data.now) : ""}">${age(r.sell_seen_at, data.now)}</td>
      <td class="num ${r.buy_max !== null && r.buy_max === maxBuy ? "good" : ""}">${fmt(r.buy_max)}</td>
      <td class="num">${r.buy_amount || "—"}</td>
      <td class="num ${r.buy_seen_at ? ageClass(r.buy_seen_at, data.now) : ""}">${age(r.buy_seen_at, data.now)}</td>
      <td class="num">${r.daily_volume === null || r.daily_volume === undefined ? "—" : fmt(r.daily_volume)}</td>
    </tr>`).join("") : `<tr><td colspan="9" class="muted">Нет данных по этому предмету.</td></tr>`;
}

// ---------- статус ----------
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
      const loc = c.location_name ? ` · ${c.location_name}` : " · локация не определена";
      el.textContent = `сборщик работает${loc}${last}${enc ? " · данные рынка зашифрованы игрой" : ""}`;
      el.className = "conn " + (enc ? "bad" : t && s.now - t.last_at < 600 ? "ok" : "old");
    } else {
      el.textContent = t ? `сборщик выключен${last}` : "сборщик выключен, данных нет";
      el.className = "conn old";
    }
  } catch { /* сервер недоступен */ }
}

async function loadStatus() {
  const s = await api("/api/status");
  $("#client-cmd").textContent = `albiondata-client.exe -i ${location.origin}${s.ingest_path}`;
  const locRows = s.locations.sort((a, b) => b.last_seen - a.last_seen).map((r) => `<tr>
      <td class="${r.location === "black_market" ? "bm" : ""}">${esc(r.name)}</td>
      <td>${r.auction_type === "offer" ? "предложения (продажа)" : "запросы (покупка)"}</td>
      <td class="num">${fmt(r.orders)}</td><td class="num">${fmt(r.items)}</td>
      <td class="num ${ageClass(r.last_seen, s.now)}">${age(r.last_seen, s.now)}</td></tr>`).join("");
  const topicRows = s.topics.map((t) => `<tr><td>${esc(t.topic)}</td><td class="num">${fmt(t.batches)}</td>
      <td class="num">${fmt(t.records)}</td><td class="num">${age(t.last_at, s.now)}</td></tr>`).join("");
  const c = s.capture;
  const capText = !c.enabled ? "выключен" : c.error ? "ошибка" : c.running ? "работает" : "остановлен";
  $("#status").innerHTML = `
    ${c.error ? `<p class="bad">Сборщик: ${esc(c.error)}</p>` : ""}
    ${c.encrypted_at ? `<p class="warn">Последний ответ рынка (${age(Math.round(c.encrypted_at), s.now)} назад) пришёл зашифрованным — игра сейчас не отдаёт цены в открытом виде.</p>` : ""}
    <div class="cards">
      <div class="card"><div class="v ${c.running ? "good" : "bad"}">${capText}</div><div class="l">встроенный сборщик · пакетов игры: ${fmt(c.packets)}</div></div>
      <div class="card"><div class="v">${esc(c.location_name || "—")}</div><div class="l">текущая локация${c.location ? "" : " — смените зону в игре"}</div></div>
      <div class="card"><div class="v">${fmt(c.order_batches)}</div><div class="l">страниц рынка собрано (${fmt(c.orders)} заказов)</div></div>
      <div class="card"><div class="v">${fmt(s.total_orders)}</div><div class="l">заказов в базе</div></div>
      <div class="card"><div class="v">${fmt(s.history_points)}</div><div class="l">точек истории продаж</div></div>
      <div class="card"><div class="v">${s.items_catalog ? fmt(s.items_catalog) : "нет"}</div><div class="l">названий предметов${s.items_catalog ? "" : " — выполните update-items"}</div></div>
    </div>
    <h2>Рынки</h2>
    <div class="table-wrap"><table><thead><tr><th>Рынок</th><th>Тип</th><th class="num">Заказов</th><th class="num">Предметов</th><th class="num">Обновлено</th></tr></thead>
      <tbody>${locRows || `<tr><td colspan="5" class="muted">Данных пока нет</td></tr>`}</tbody></table></div>
    <h2>Полученные пакеты</h2>
    <div class="table-wrap"><table><thead><tr><th>Топик</th><th class="num">Пакетов</th><th class="num">Записей</th><th class="num">Последний</th></tr></thead>
      <tbody>${topicRows || `<tr><td colspan="4" class="muted">Клиент ещё ничего не присылал</td></tr>`}</tbody></table></div>`;
}

// ---------- быстрая продажа ----------
function initFastSell(data) {
  const s = loadSettings(FS_STORE_KEY);
  const f = $("#fs-filters");
  checkboxList($("#fs-locs"), "fsloc",
    locations.map((l) => ({ value: l.key, label: l.name, cls: l.kind === "black_market" ? "bm" : "" })),
    s.locs || [...data.default_cities, "black_market"]);
  checkboxList($("#fs-tiers"), "fstier", [2, 3, 4, 5, 6, 7, 8].map((t) => ({ value: String(t), label: `T${t}` })), s.tiers || []);
  checkboxList($("#fs-enchants"), "fsench", [0, 1, 2, 3, 4].map((e) => ({ value: String(e), label: `.${e}` })), s.enchants || []);
  for (const l of locations) f.base.insertAdjacentHTML("beforeend", `<option value="${esc(l.key)}">${esc(l.name)}</option>`);
  for (const k of ["base", "max_age", "min_gain", "min_markets", "q"]) if (s[k] !== undefined) f[k].value = s[k];
  if (s.premium !== undefined) f.premium.checked = s.premium;
  f.addEventListener("submit", (e) => { e.preventDefault(); loadFastSell(); });
  f.addEventListener("change", () => saveSettings(readFastSell(), FS_STORE_KEY));
}

function readFastSell() {
  const f = $("#fs-filters");
  const vals = (name) => [...f.querySelectorAll(`input[name=${name}]:checked`)].map((i) => i.value);
  return {
    locs: vals("fsloc"), tiers: vals("fstier"), enchants: vals("fsench"), base: f.base.value,
    premium: f.premium.checked, max_age: f.max_age.value, min_gain: f.min_gain.value,
    min_markets: f.min_markets.value, q: f.q.value,
  };
}

async function loadFastSell() {
  fastSellLoaded = true;
  const f = readFastSell();
  saveSettings(f, FS_STORE_KEY);
  const summary = $("#fs-summary");
  summary.textContent = "Загрузка…";
  try {
    const data = await api("/api/fastsell", {
      locs: f.locs.join(","), tiers: f.tiers.join(","), enchants: f.enchants.join(","), base: f.base,
      premium: f.premium ? 1 : 0, max_age: f.max_age, min_gain: f.min_gain, min_markets: f.min_markets,
      q: f.q, limit: 500,
    });
    const shown = data.rows.length < data.count ? ` (показано ${data.rows.length})` : "";
    summary.textContent = `Предметов: ${data.count}${shown}. Цена в ячейке — лучший заказ на покупку; «на руки» — после налога ${(data.tax * 100).toFixed(1)}%. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.`;
    renderFastSell(data);
  } catch (e) {
    summary.innerHTML = `<span class="bad">Ошибка: ${esc(e.message)}</span>`;
  }
}

function renderFastSell(data) {
  const table = $("#fastsell");
  const gainTitle = data.base ? `Выгода vs ${locNames[data.base] || data.base}` : "Разница лучший−худший";
  table.querySelector("thead").innerHTML = `<tr><th>Предмет</th><th>Кач.</th>
    ${data.locations.map((l) => `<th class="num ${l === "black_market" ? "bm" : ""}">${esc(locNames[l] || l)}</th>`).join("")}
    <th>Лучший рынок</th><th class="num">На руки</th><th class="num">${esc(gainTitle)}</th><th class="num">Спред</th><th class="num">Объём/сут</th></tr>`;
  const tbody = table.querySelector("tbody");
  if (!data.rows.length) {
    tbody.innerHTML = `<tr><td colspan="${data.locations.length + 7}" class="muted">Нет данных. Откройте в игре заказы на покупку (вкладка «Продать») на нескольких рынках или ослабьте фильтры.</td></tr>`;
    return;
  }
  tbody.innerHTML = data.rows.map((r) => {
    const cells = data.locations.map((l) => {
      const c = r.cells[l];
      const cls = [l === r.best_location ? "best" : "", l === data.base ? "base" : ""].join(" ");
      if (!c) return `<td class="num muted ${cls}">—</td>`;
      const from = c.quality !== r.quality ? `, заказ кач. ${c.quality}` : "";
      return `<td class="num ${cls}" title="${c.amount} шт.${from}, ${age(c.seen_at, data.now)} назад">${fmt(c.price)}<span class="sub ${ageClass(c.seen_at, data.now)}">${age(c.seen_at, data.now)}</span></td>`;
    }).join("");
    const gain = data.base ? r.gain_vs_base : r.spread;
    return `<tr>
      <td><span class="item-name" data-item="${esc(r.item_id)}">${esc(r.name)}</span><br><span class="item-id">${esc(r.item_id)}</span></td>
      <td title="${QUALITY[r.quality] || ""}">${r.quality}</td>
      ${cells}
      <td class="${r.best_location === "black_market" ? "bm" : ""}">${esc(locNames[r.best_location] || r.best_location)}</td>
      <td class="num">${fmt(r.best_net)}</td>
      <td class="num good"><b>${gain === null ? "—" : fmt(gain)}</b></td>
      <td class="num">${r.spread_pct.toFixed(1)}%</td>
      <td class="num">${r.daily_volume === null || r.daily_volume === undefined ? "—" : fmt(r.daily_volume)}</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll(".item-name").forEach((el) => el.addEventListener("click", () => {
    $("#price-form").item.value = el.dataset.item;
    showTab("prices");
    loadPrices();
  }));
}

init().catch((e) => { $("#deals-summary").innerHTML = `<span class="bad">Не удалось загрузить: ${esc(e.message)}</span>`; });
