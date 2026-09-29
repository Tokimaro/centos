"use strict";
// Торговля: сделки между рынками, быстрая продажа, цены предмета.

const STORE_KEY = "albion-trader-settings";
const FS_STORE_KEY = "albion-trader-fastsell";
let lastDeals = [];
let sortKey = "total_profit";
let sortAsc = false;
let autoTimer = null;

// ---------- фильтры ----------
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

function initDeals() {
  const s = loadSettings(STORE_KEY);
  const data = { default_cities: App.defaultCities };
  const locations = App.locations;
  const buyable = locations.filter((l) => l.kind !== "black_market");
  const cities = data.default_cities;
  checkboxList($("#src-list"), "src", buyable.map((l) => ({ value: l.key, label: l.name })), s.src || cities);
  checkboxList($("#dst-list"), "dst",
    locations.map((l) => ({ value: l.key, label: l.name, cls: l.kind === "black_market" ? "bm" : "" })),
    s.dst || [...cities, "black_market"]);
  checkboxList($("#tiers"), "tier", [2, 3, 4, 5, 6, 7, 8].map((t) => ({ value: String(t), label: `T${t}` })), s.tiers || []);
  checkboxList($("#enchants"), "enchant", [0, 1, 2, 3, 4].map((e) => ({ value: String(e), label: `.${e}` })), s.enchants || []);
  applySettings(s);

  $("#preset-fastsell").addEventListener("click", () => {
    const f = $("#filters");
    f.buy.value = "order";
    f.sell.value = "instant";
    loadDeals();
  });
  $("#filters").addEventListener("submit", (e) => { e.preventDefault(); loadDeals(); });
  $("#filters").addEventListener("change", () => { saveSettings(readFilters(), STORE_KEY); scheduleAuto(); });
  document.querySelectorAll("#deals th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = ["name", "source", "age"].includes(k); }
    renderDeals();
  }));
  loadDeals();
  scheduleAuto();
}

function scheduleAuto() {
  clearInterval(autoTimer);
  if ($("#filters").auto.checked) autoTimer = setInterval(loadDeals, 30000);
}

// ---------- сделки ----------
async function loadDeals() {
  const f = readFilters();
  saveSettings(f, STORE_KEY);
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
    if (sortKey === "source") return `${App.locNames[d.source] || d.source} ${App.locNames[d.destination] || d.destination}`;
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
      <td>${esc(App.locNames[d.source] || d.source)} → <span class="${dstCls}">${esc(App.locNames[d.destination] || d.destination)}</span></td>
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
  tbody.querySelectorAll(".item-name").forEach((el) => el.addEventListener("click", () => App.openItem(el.dataset.item)));
}

// ---------- быстрая продажа ----------
function initFastSell() {
  const data = { default_cities: App.defaultCities };
  const locations = App.locations;
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
  const gainTitle = data.base ? `Выгода vs ${App.locNames[data.base] || data.base}` : "Разница лучший−худший";
  table.querySelector("thead").innerHTML = `<tr><th>Предмет</th><th>Кач.</th>
    ${data.locations.map((l) => `<th class="num ${l === "black_market" ? "bm" : ""}">${esc(App.locNames[l] || l)}</th>`).join("")}
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
      <td class="${r.best_location === "black_market" ? "bm" : ""}">${esc(App.locNames[r.best_location] || r.best_location)}</td>
      <td class="num">${fmt(r.best_net)}</td>
      <td class="num good"><b>${gain === null ? "—" : fmt(gain)}</b></td>
      <td class="num">${r.spread_pct.toFixed(1)}%</td>
      <td class="num">${r.daily_volume === null || r.daily_volume === undefined ? "—" : fmt(r.daily_volume)}</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll(".item-name").forEach((el) => el.addEventListener("click", () => App.openItem(el.dataset.item)));
}

// ---------- цены предмета ----------
async function suggestItems() {
  const q = $("#price-form").item.value.trim();
  if (q.length < 2) return;
  const data = await api("/api/items", { q, limit: 30 });
  $("#item-options").innerHTML = data.items.map((i) => `<option value="${esc(i.item_id)}">${esc(i.name)}</option>`).join("");
}

async function loadChart() {
  const f = $("#chart-form");
  const item = $("#price-form").item.value.trim();
  if (!item || !f.location.value) return;
  const data = await api("/api/history", { item, location: f.location.value, quality: f.quality.value, days: f.days.value });
  lineChart($("#price-chart"), [
    { name: "Мин. цена продажи", slot: 1, points: data.snapshots.map((r) => [r.ts, r.sell_min]) },
    { name: "Макс. цена покупки", slot: 2, points: data.snapshots.map((r) => [r.ts, r.buy_max]) },
    { name: "Средняя цена сделок", slot: 3, points: data.sales.map((r) => [r.ts, r.avg_price]) },
  ], { empty: "История пока пуста: снимки цен копятся при каждом просмотре рынка, средняя цена сделок — когда вы открываете график цен предмета в игре." });
}

function fillChartControls(rows) {
  const f = $("#chart-form");
  const prevLoc = f.location.value, prevQ = f.quality.value;
  const locs = [...new Set(rows.map((r) => r.location))];
  const qs = [...new Set(rows.map((r) => r.quality))].sort();
  f.location.innerHTML = locs.map((l) => `<option value="${esc(l)}">${esc(App.locName(l))}</option>`).join("");
  f.quality.innerHTML = qs.map((q) => `<option value="${q}">${esc(QUALITY[q] || q)}</option>`).join("");
  if (locs.includes(prevLoc)) f.location.value = prevLoc;
  if (qs.map(String).includes(prevQ)) f.quality.value = prevQ;
  $("#chart-wrap").hidden = !locs.length;
  if (locs.length) loadChart();
}

async function loadRecent() {
  $("#chart-wrap").hidden = true;
  $("#price-title").textContent = "";
  $("#prices-wrap").hidden = true;
  $("#recent-wrap").hidden = false;
  const data = await api("/api/items", { recent: 1, limit: 100 });
  const tbody = $("#recent tbody");
  tbody.innerHTML = data.items.length ? data.items.map((i) => `<tr>
      <td><span class="item-name" data-item="${esc(i.item_id)}">${esc(i.name)}</span><br><span class="item-id">${esc(i.item_id)}</span></td>
      <td class="num">${i.markets}</td><td class="num">${fmt(i.orders)}</td>
      <td class="num ${ageClass(i.last_seen, data.now)}">${age(i.last_seen, data.now)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">Пока ничего не собрано. Откройте рынок в игре (после смены зоны).</td></tr>`;
  tbody.querySelectorAll(".item-name").forEach((el) => el.addEventListener("click", () => {
    $("#price-form").item.value = el.dataset.item;
    loadPrices();
  }));
}

async function loadPrices() {
  const f = $("#price-form");
  let item = f.item.value.trim();
  if (!item) { loadRecent(); return; }
  $("#prices-wrap").hidden = false;
  $("#recent-wrap").hidden = true;
  if (!/^[A-Z0-9_@]+$/.test(item)) {
    const found = await api("/api/items", { q: item, limit: 1 });
    if (found.items.length) { item = found.items[0].item_id; f.item.value = item; }
  }
  const data = await api("/api/prices", { item, max_age: f.max_age.value });
  $("#price-title").textContent = `${data.name} (${data.item_id})`;
  fillChartControls(data.rows);
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


App.tab({ id: "deals", group: "trade", title: "Сделки", init: initDeals });
App.tab({ id: "fastsell", group: "trade", title: "Быстрая продажа", init() { initFastSell(); loadFastSell(); } });
App.tab({
  id: "prices", group: "trade", title: "Цены предмета",
  init() {
    $("#price-form").addEventListener("submit", (e) => { e.preventDefault(); loadPrices(); });
    $("#price-form").item.addEventListener("input", debounce(suggestItems, 250));
    $("#chart-form").addEventListener("change", loadChart);
    window.addEventListener("resize", debounce(() => { if (!$("#chart-wrap").hidden) loadChart(); }, 300));
  },
  show() { if (!$("#price-form").item.value.trim()) loadRecent(); },
});

// ---------- флиппинг внутри города ----------
standardTab({
  id: "flips", group: "trade", title: "Флиппинг",
  intro: "Перепродажа внутри одного города без перевозки: ставите заказ на покупку на 1 серебро выше лучшего, "
    + "купленное выставляете на 1 серебро дешевле лучшего предложения. Учтены налог и две комиссии за размещение. "
    + "«Потенциал/сут» — прибыль × средние продажи в сутки (нужна история цен).",
  spec: [
    { legend: "Рынки", cls: "locs", fields: [{ type: "markets", name: "locs", buyable: true }] },
    { legend: "Параметры", fields: [
      { type: "check", name: "premium", label: "Премиум (налог 4%, без — 8%)", value: true },
      { type: "number", name: "max_age", label: "Свежесть данных, ч", value: 6, min: 0.1 },
      { type: "number", name: "min_margin", label: "Мин. маржа, %", value: 10, min: 0 },
      { type: "number", name: "min_profit", label: "Мин. прибыль / шт", value: 500, min: 0 },
      { type: "number", name: "min_volume", label: "Мин. продаж в сутки", value: 0, min: 0 },
    ] },
    { legend: "Предметы", fields: [
      { type: "search", name: "q", label: "Поиск", placeholder: "название или ID" },
      { type: "tiers" }, { type: "enchants" },
    ] },
  ],
  columns: [
    { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
    { key: "quality", title: "Кач.", html: (r) => `<span title="${QUALITY[r.quality] || ""}">${r.quality}</span>` },
    { key: "location", title: "Рынок", html: (r) => locCell(r.location), sort: (r) => App.locName(r.location) },
    { key: "bid", title: "Лучший заказ", num: true, html: (r) => fmt(r.bid), hint: "Лучший заказ на покупку — ставите на 1 выше" },
    { key: "ask", title: "Лучшее предл.", num: true, html: (r) => fmt(r.ask), hint: "Лучшее предложение — выставляете на 1 ниже" },
    { key: "profit", title: "Прибыль/шт", num: true, html: (r) => fmt(r.profit), cls: () => "good" },
    { key: "margin", title: "Маржа", num: true, html: (r) => pct(r.margin) },
    { key: "spread_pct", title: "Спред", num: true, html: (r) => pct(r.spread_pct) },
    { key: "daily_volume", title: "Продаж/сут", num: true, html: (r) => fmt(r.daily_volume) },
    { key: "daily_potential", title: "Потенциал/сут", num: true, html: (r) => r.daily_potential === null ? "—" : `<b>${fmt(r.daily_potential)}</b>`, cls: () => "good" },
    { key: "instant_profit", title: "Мгновенно", num: true, hint: "Лучшее предложение дешевле заказа на покупку после налога — можно купить и сразу сдать",
      html: (r) => r.instant_profit ? `<span class="pill good">+${fmt(r.instant_profit)}</span>` : "" },
    { key: "seen_at", title: "Возраст", num: true, html: (r) => `<span class="${ageClass(r.seen_at, r._now)}">${age(r.seen_at, r._now)}</span>`, sort: (r) => -r.seen_at },
  ],
  sort: "daily_potential",
  empty: "Нет вариантов. Флиппингу нужны и заказы на покупку, и предложения одного рынка — откройте в игре обе вкладки рынка или ослабьте фильтры.",
  async load(f) {
    const data = await api("/api/flips", { ...f, premium: f.premium, limit: 500 });
    data.rows.forEach((r) => { r._now = data.now; });
    return { rows: data.rows, summary: `Вариантов: ${data.count}${data.rows.length < data.count ? ` (показано ${data.rows.length})` : ""}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});

// ---------- недооценённые лоты ----------
standardTab({
  id: "underpriced", group: "trade", title: "Недооценённые",
  intro: "Предложения заметно дешевле обычной цены — кто-то ошибся или сливает товар. Опорная цена — медиана "
    + "минимальных цен за 7 дней по снимкам (нужно ≥3 снимков), иначе средняя цена сделок. Прибыль — если купить "
    + "и перевыставить по опорной цене своим предложением (налог и комиссия учтены).",
  spec: [
    { legend: "Рынки", cls: "locs", fields: [{ type: "markets", name: "locs", buyable: true }] },
    { legend: "Параметры", fields: [
      { type: "check", name: "premium", label: "Премиум (налог 4%, без — 8%)", value: true },
      { type: "number", name: "max_age", label: "Свежесть предложений, ч", value: 2, min: 0.1 },
      { type: "number", name: "min_discount", label: "Мин. скидка, %", value: 20, min: 0 },
      { type: "number", name: "min_profit", label: "Мин. прибыль / шт", value: 1000, min: 0 },
    ] },
    { legend: "Предметы", fields: [
      { type: "search", name: "q", label: "Поиск", placeholder: "название или ID" },
      { type: "tiers" }, { type: "enchants" },
    ] },
  ],
  columns: [
    { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
    { key: "quality", title: "Кач.", html: (r) => `<span title="${QUALITY[r.quality] || ""}">${r.quality}</span>` },
    { key: "location", title: "Рынок", html: (r) => locCell(r.location), sort: (r) => App.locName(r.location) },
    { key: "price", title: "Цена", num: true, html: (r) => fmt(r.price) },
    { key: "ref_price", title: "Обычно", num: true, html: (r) => `<span title="${r.ref_source === "median" ? `медиана ${r.ref_points} снимков` : `средняя цена сделок, ${r.ref_points} дн.`}">${fmt(r.ref_price)}</span>` },
    { key: "discount", title: "Скидка", num: true, html: (r) => `<b>${pct(r.discount)}</b>`, cls: () => "good" },
    { key: "amount", title: "Шт.", num: true, html: (r) => fmt(r.amount) },
    { key: "profit", title: "Прибыль/шт", num: true, html: (r) => fmt(r.profit), cls: () => "good" },
    { key: "total_profit", title: "Итого", num: true, html: (r) => `<b>${fmt(r.total_profit)}</b>`, cls: () => "good" },
    { key: "seen_at", title: "Возраст", num: true, html: (r) => `<span class="${ageClass(r.seen_at, r._now)}">${age(r.seen_at, r._now)}</span>`, sort: (r) => -r.seen_at },
  ],
  sort: "total_profit",
  empty: "Недооценённых лотов нет. Для опорной цены нужна история: один и тот же рынок надо посмотреть хотя бы 3 раза с интервалом (или открыть график цен предмета в игре).",
  async load(f) {
    const data = await api("/api/underpriced", { ...f, limit: 500 });
    data.rows.forEach((r) => { r._now = data.now; });
    return { rows: data.rows, summary: `Лотов: ${data.count}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});

// ---------- спрос Чёрного рынка ----------
standardTab({
  id: "bmdemand", group: "trade", title: "Спрос ЧР",
  intro: "Что Чёрный рынок покупает чаще и дороже — по всем вашим просмотрам ЧР за период и истории его продаж. "
    + "«Маржа» — последняя цена ЧР после налога минус самое дешёвое предложение в городах (качество не ниже). "
    + "Рейтинг = средняя цена × продажи в сутки (или число наблюдений, если истории нет).",
  spec: [
    { legend: "Период", fields: [
      { type: "select", name: "days", label: "За", value: "7",
        options: [["1", "сутки"], ["3", "3 дня"], ["7", "неделю"], ["30", "месяц"]] },
      { type: "check", name: "premium", label: "Премиум (налог 4%, без — 8%)", value: true },
      { type: "number", name: "max_age", label: "Цены городов не старше, ч", value: 24, min: 0.1 },
      { type: "number", name: "min_margin", label: "Мин. маржа (пусто — все)", min: 0 },
    ] },
    { legend: "Предметы", fields: [
      { type: "search", name: "q", label: "Поиск", placeholder: "название или ID" },
      { type: "tiers" }, { type: "enchants" },
    ] },
  ],
  columns: [
    { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
    { key: "quality", title: "Кач.", html: (r) => `<span title="${QUALITY[r.quality] || ""}">${r.quality}</span>` },
    { key: "last_price", title: "Цена ЧР", num: true, html: (r) => fmt(r.last_price), hint: "Последний лучший заказ ЧР" },
    { key: "avg_price", title: "Средняя", num: true, html: (r) => fmt(r.avg_price) },
    { key: "max_price", title: "Макс.", num: true, html: (r) => fmt(r.max_price) },
    { key: "days_seen", title: "Дней с заказом", num: true, html: (r) => `${r.days_seen} <span class="muted">(${r.snapshots})</span>`, hint: "В скольких днях периода был заказ ЧР (в скобках — число снимков)" },
    { key: "daily_sold", title: "Продаж/сут", num: true, html: (r) => fmt(r.daily_sold) },
    { key: "city_price", title: "Дешевле всего в городе", num: true, html: (r) => r.city ? `${fmt(r.city_price)} <span class="muted">${esc(App.locName(r.city))}</span>` : "—" },
    { key: "margin", title: "Маржа", num: true, html: (r) => r.margin === null ? "—" : `<b>${fmt(r.margin)}</b>`, cls: (r) => r.margin > 0 ? "good" : r.margin < 0 ? "bad" : "" },
    { key: "score", title: "Рейтинг", num: true, html: (r) => fmt(r.score) },
    { key: "last_seen", title: "Последний раз", num: true, html: (r) => `<span class="${ageClass(r.last_seen, r._now)}">${age(r.last_seen, r._now)}</span>`, sort: (r) => r.last_seen || 0 },
  ],
  sort: "score",
  empty: "Данных нет. Спрос копится, когда вы открываете Чёрный рынок в Карлеоне (и график цен предметов на нём).",
  async load(f) {
    const data = await api("/api/bm-demand", { ...f, limit: 500 });
    data.rows.forEach((r) => { r._now = data.now; });
    return { rows: data.rows, summary: `Предметов: ${data.count}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});
