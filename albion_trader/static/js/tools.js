"use strict";
// Инструменты: работают и в основном интерфейсе (группа «Инструменты»),
// и в окне-компаньоне (companion.html загружает только core.js и этот файл).

// Кнопка «Отдельное окно»: на этом компьютере сервер открывает окно-приложение
// (можно закрепить поверх игры); с другого устройства — всплывающее окно браузера.
async function openCompanion() {
  try {
    const r = await apiPost("/api/window", { action: "open" });
    if (r.mode && r.mode !== "popup") return;   // popup: откроет этот браузер
  } catch { /* не этот компьютер — обычное всплывающее окно */ }
  window.open("companion.html", "albion-companion", "popup,width=560,height=860");
}

// Форма инструмента в раскрывающемся блоке «Параметры»: в узком окне результат
// виден сразу; состояние (раскрыт/свёрнут) запоминается.
function toolForm(el, key, spec, opts) {
  const box = document.createElement("details");
  box.className = "tool-form";
  const state = loadSettings("albion-trader-tool-forms");
  box.open = state[key] !== undefined ? state[key] : !document.body.classList.contains("companion");
  box.innerHTML = "<summary>Параметры</summary>";
  box.addEventListener("toggle", () => saveSettings({ ...loadSettings("albion-trader-tool-forms"), [key]: box.open }, "albion-trader-tool-forms"));
  el.appendChild(box);
  return buildForm(box, key, spec, opts);
}

// Небольшая карточка «значение + подпись».
function card(value, label, cls = "") {
  return `<div class="card"><div class="v ${cls}">${value}</div><div class="l">${esc(label)}</div></div>`;
}

// ---------- Сейчас: зона, персонаж, итоги сессии ----------
App.tab({
  id: "now", group: "tools", title: "Сейчас", live: true,
  init(el) {
    el.innerHTML = `<p class="muted intro">Текущая зона и итоги сессии обновляются сами каждые 20 секунд.</p>
      <div class="cards" id="now-cards"></div><div id="now-loot"></div>`;
    this.refresh(el);
  },
  async refresh(el) {
    try {
      const [st, se] = await Promise.all([api("/api/status"), api("/api/session")]);
      const c = st.capture || {};
      $("#now-cards", el).innerHTML = [
        card(esc(c.zone_name || "—"), "зона"),
        card(esc(c.character || "—"), "персонаж"),
        card(fmt(se.fame_per_hour), "слава в час"),
        card(fmt(se.silver_per_hour), "серебро в час"),
        card(fmt(se.loot_value_per_hour), "лут в час (оценка)"),
        card(se.hours ? `${fmt1(se.hours)} ч` : "—", "длительность сессии"),
      ].join("");
      const items = (se.items || []).slice(0, 5);
      $("#now-loot", el).innerHTML = items.length ? `<h2>Лучший лут сессии</h2><ul>${items.map((i) =>
        `<li>${esc(i.name || i.item_id)} × ${fmt(i.amount)} — ${fmt(i.value)}</li>`).join("")}</ul>` : "";
    } catch (e) { $("#now-cards", el).innerHTML = `<p class="bad">Ошибка: ${esc(e.message)}</p>`; }
  },
});

// Поле предмета с подсказками по всему справочнику.
function itemPicker(input, extra = {}) {
  const id = `dl-${Math.random().toString(36).slice(2)}`;
  const list = document.createElement("datalist");
  list.id = id;
  input.setAttribute("list", id);
  input.after(list);
  input.placeholder = input.placeholder || "начните вводить название";
  input.addEventListener("input", debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    try {
      const data = await api("/api/items", { catalog: 1, q, limit: 40, ...extra });
      list.innerHTML = data.items.map((i) => `<option value="${esc(i.item_id)}">${esc(i.name)}</option>`).join("");
    } catch { /* подсказки необязательны */ }
  }, 250));
}

const QUALITY_OPTIONS = Object.entries(QUALITY).map(([k, v]) => [k, v]);
const MODE_BUY = [["instant", "мгновенно (из предложений)"], ["order", "своим заказом"]];
const MODE_SELL = [["instant", "мгновенно (в запросы)"], ["order", "своим предложением"]];

// ---------- Цепочка производства ----------
App.tab({
  id: "chain", group: "tools", title: "Цепочка",
  init(el) {
    const form = toolForm(el, "albion-trader-chain", [
      { legend: "Предмет", fields: [
        { type: "text", name: "item", label: "Предмет (название или ID)", value: "T4_BAG" },
        { type: "number", name: "qty", label: "Количество", value: 10, min: 1, step: 1 },
        { type: "select", name: "quality", label: "Качество при продаже", options: QUALITY_OPTIONS, value: "1" },
      ] },
      { legend: "Покупать ресурсы (берётся самый дешёвый рынок)", cls: "locs", fields: [
        { type: "markets", name: "buy_markets", buyable: true },
        { type: "select", name: "buy_mode", label: "Покупка", options: MODE_BUY },
      ] },
      { legend: "Производство", fields: [
        { type: "market", name: "craft_city", label: "Город крафта", buyable: true, value: "martlock" },
        { type: "market", name: "refine_city", label: "Город переработки", buyable: true, value: "martlock" },
        { type: "check", name: "focus", label: "С фокусом" },
        { type: "number", name: "station_fee", label: "Плата станции за 100 питания", value: 0, min: 0 },
      ] },
      { legend: "Продажа", fields: [
        { type: "market", name: "sell_market", label: "Продавать в", value: "martlock" },
        { type: "select", name: "sell_mode", label: "Продажа", options: MODE_SELL },
        { type: "check", name: "premium", label: "Премиум (налог 4%)", value: true },
        { type: "number", name: "max_age", label: "Свежесть цен, ч", value: 24, min: 0.1 },
      ] },
    ], { submitText: "Рассчитать", onSubmit: () => this.refresh(el),
      intro: "Для каждого ресурса сравнивается «купить» и «сделать самому» (с возвратом ресурсов по бонусу города и фокусу) — выбирается дешевле. Раскройте строку, чтобы увидеть, из чего она делается." });
    itemPicker(form.form.querySelector("[name=item]"), { craftable: 1 });
    this.form = form;
    el.insertAdjacentHTML("beforeend", `<div class="summary" id="chain-summary"></div><div class="cards" id="chain-cards"></div>
      <div class="table-wrap"><table id="chain-tree"></table></div><h2>Что купить</h2><div id="chain-shop"></div>`);
    this.shop = makeTable($("#chain-shop", el), [
      { key: "name", title: "Предмет", html: (r) => itemCell(r) },
      { key: "market", title: "Где", html: (r) => locCell(r.market) },
      { key: "qty", title: "Кол-во", num: true, html: (r) => fmt1(r.qty) },
      { key: "unit", title: "Цена/шт", num: true, html: (r) => fmt(r.unit) },
      { key: "total", title: "Итого", num: true, html: (r) => fmt(r.total) },
    ], { sort: "total", empty: "Покупать нечего — всё делается самим" });
    this.refresh(el);
  },
  async refresh(el) {
    const p = this.form.read();
    if (!p.item) return;
    const sum = $("#chain-summary", el);
    try {
      const r = await api("/api/chain", p);
      if (r.no_gamedata) { sum.innerHTML = `<span class="warn">Нет рецептов — выполните update-items (или дождитесь загрузки справочников).</span>`; return; }
      sum.innerHTML = `${esc(r.name)} × ${fmt(r.qty)}` + (r.has_recipe ? "" : ` — <span class="warn">у предмета нет рецепта, только покупка</span>`)
        + (r.missing.length ? `<br><span class="warn">Нет цен: ${esc(r.missing_names.join(", "))} — откройте их на рынке в игре.</span>` : "");
      const cls = r.profit > 0 ? "good" : r.profit < 0 ? "bad" : "";
      $("#chain-cards", el).innerHTML = [
        card(fmt(r.cost), "себестоимость"), card(fmt(r.cost_unit), "за штуку"),
        card(fmt(r.revenue), "выручка после налогов"), card(fmt(r.profit), "прибыль", cls),
        card(pct(r.margin), "маржа", cls), card(fmt(r.focus), "фокус"),
      ].join("");
      this.renderTree($("#chain-tree", el), r.tree);
      this.shop.set(r.shopping);
    } catch (e) { summaryLine(sum, "", e.message); }
  },
  renderTree(table, tree) {
    const open = new Set();
    const collect = (n, path) => { if (n.decision === "craft") { open.add(path); n.children.forEach((c, i) => collect(c, `${path}.${i}`)); } };
    collect(tree, "0");
    const label = { craft: ["сделать", "good"], buy: ["купить", ""], missing: ["нет цены", "bad"] };
    const draw = () => {
      const rows = [];
      const walk = (n, path) => {
        const [text, cls] = label[n.decision];
        const has = n.children.length > 0;
        const toggle = has ? `<button type="button" class="tree-toggle" data-path="${path}">${open.has(path) ? "▾" : "▸"}</button>` : `<span class="tree-toggle"></span>`;
        const buy = n.buy_unit !== null ? `${fmt(n.buy_unit)}<span class="sub">${esc(App.locName(n.buy_market))}</span>` : "—";
        const craft = n.craft_unit !== null ? `${fmt(n.craft_unit)}<span class="sub">${n.kind === "refine" ? "переработка" : "крафт"}, возврат ${fmt1(n.return_rate)}%</span>` : (has ? "—" : "");
        rows.push(`<tr class="${n.decision === "missing" ? "bad" : ""}"><td style="padding-left:${6 + n.depth * 18}px">${toggle}<span class="item-name" data-item="${esc(n.item_id)}">${esc(n.name || n.item_id)}</span></td>
          <td class="num">${fmt1(n.qty)}</td><td class="num">${buy}</td><td class="num">${craft}</td>
          <td><span class="pill ${cls}">${text}</span></td><td class="num">${fmt(n.best_total)}</td></tr>`);
        if (has && open.has(path)) n.children.forEach((c, i) => walk(c, `${path}.${i}`));
      };
      walk(tree, "0");
      table.innerHTML = `<thead><tr><th>Предмет</th><th class="num">Кол-во</th><th class="num">Купить, шт</th><th class="num">Сделать, шт</th><th>Решение</th><th class="num">Итого</th></tr></thead><tbody>${rows.join("")}</tbody>`;
      $$(".tree-toggle[data-path]", table).forEach((b) => b.addEventListener("click", () => {
        open.has(b.dataset.path) ? open.delete(b.dataset.path) : open.add(b.dataset.path); draw();
      }));
      $$(".item-name", table).forEach((x) => x.addEventListener("click", () => App.openItem(x.dataset.item)));
    };
    draw();
  },
});

// ---------- Конструктор билдов ----------
const BUILD_DRAFT = "albion-trader-build-draft";
const BUILD_IMPORT = "albion-trader-build-import";

App.tab({
  id: "build", group: "tools", title: "Билд",
  init(el) {
    this.slots = [];
    this.id = null;
    el.innerHTML = `<p class="muted intro">Соберите комплект — увидите цену в каждом городе, где он дешевле всего по слотам и среднюю силу предметов (без учёта мастерства). Качество: берутся предложения не хуже выбранного.</p>
      <div class="buttons build-bar">
        <select id="build-list" title="Сохранённые билды"></select>
        <input type="text" id="build-name" placeholder="Название билда">
        <button type="button" class="secondary" id="build-save">Сохранить</button>
        <button type="button" class="secondary" id="build-new">Новый</button>
        <button type="button" class="secondary" id="build-del">Удалить</button>
      </div>
      <div id="build-slots" class="build-slots"></div>`;
    const form = toolForm(el, "albion-trader-build", [
      { legend: "Где покупать", cls: "locs", fields: [{ type: "markets", name: "markets", buyable: true }] },
      { legend: "Цены", fields: [{ type: "number", name: "max_age", label: "Свежесть цен, ч", value: 48, min: 0.1 }] },
    ], { submitText: "Посчитать", onSubmit: () => this.calc(el) });
    this.form = form;
    el.insertAdjacentHTML("beforeend", `<div class="buttons"><button type="button" class="primary" id="build-calc">Посчитать</button></div>
      <div class="summary" id="build-summary"></div><div class="cards" id="build-cards"></div>
      <h2>По слотам</h2><div id="build-rows"></div><h2>По городам</h2><div id="build-markets"></div>`);
    this.rowsTable = makeTable($("#build-rows", el), [
      { key: "slot_name", title: "Слот" },
      { key: "name", title: "Предмет", html: (r) => `${itemCell(r)}${r.spec_name ? `<a href="#" class="sub spec-link" data-node="${esc(r.spec)}" data-title="${esc(r.spec_name)}" title="Отслеживать на Доске судьбы">${esc(r.spec_name)}</a>` : ""}` },
      { key: "ip", title: "Сила", num: true, html: (r) => fmt(r.ip) },
      { key: "best_price", title: "Дешевле всего", num: true, html: (r) => r.best_price === null ? `<span class="bad">нет цены</span>`
        : `${fmt(r.best_price)}<span class="sub">${esc(App.locName(r.best_market))}${r.count > 1 ? ` · ${r.count} шт` : ""}</span>` },
    ], { empty: "Добавьте предметы в слоты" });
    this.marketTable = makeTable($("#build-markets", el), [
      { key: "name", title: "Рынок", html: (r) => locCell(r.market) },
      { key: "total", title: "Сумма", num: true, html: (r) => r.total ? fmt(r.total) + (r.complete ? "" : `<span class="sub">без недостающих</span>`) : "—" },
      { key: "missing", title: "Не хватает", sort: (r) => r.missing.length,
        html: (r) => r.missing.length ? `<span class="warn">${esc(r.missing.map((s) => this.slotName(s)).join(", "))}</span>` : `<span class="good">всё есть</span>` },
    ], { empty: "—" });
    $("#build-calc", el).addEventListener("click", () => this.calc(el));
    $("#build-rows", el).addEventListener("click", (e) => {
      const a = e.target.closest(".spec-link");
      if (!a) return;
      e.preventDefault();
      saveSettings({ node: a.dataset.node, title: a.dataset.title }, DESTINY_OPEN);
      App.go("destiny");
    });
    $("#build-save", el).addEventListener("click", () => this.save(el));
    $("#build-new", el).addEventListener("click", () => { this.id = null; this.fill(el, { name: "", slots: {} }); this.renderList(el); this.saveDraft(el); });
    $("#build-del", el).addEventListener("click", () => this.remove(el));
    $("#build-list", el).addEventListener("change", (e) => {
      const b = this.saved.find((x) => String(x.id) === e.target.value);
      if (b) { this.id = b.id; this.fill(el, b); this.calc(el); } else { this.id = null; }
      this.saveDraft(el);
    });
    this.load(el);
  },
  show(el) {
    // Билд, переданный из «Меты по киллборду».
    const imported = loadSettings(BUILD_IMPORT);
    if (imported.slots && this.slots.length) {
      saveSettings({}, BUILD_IMPORT);
      this.id = null;
      this.fill(el, imported);
      this.calc(el);
    }
  },
  slotName(key) { return (this.slots.find(([k]) => k === key) || [key, key])[1]; },
  async load(el) {
    const data = await api("/api/builds");
    this.slots = data.slots;
    this.saved = data.builds;
    this.renderList(el);
    const draft = loadSettings(BUILD_DRAFT);
    this.id = this.saved.some((b) => b.id === draft.id) ? draft.id : null;
    this.renderList(el);
    this.fill(el, draft.slots ? draft : { name: "", slots: {} });
    this.show(el);
    if (Object.keys(this.read(el).slots).length) this.calc(el);
  },
  renderList(el) {
    $("#build-list", el).innerHTML = `<option value="">— сохранённые билды —</option>` + this.saved.map((b) =>
      `<option value="${b.id}"${b.id === this.id ? " selected" : ""}>${esc(b.name)}</option>`).join("");
  },
  fill(el, build) {
    $("#build-name", el).value = build.name || "";
    const box = $("#build-slots", el);
    box.innerHTML = this.slots.map(([key, title]) => {
      const s = (build.slots || {})[key] || {};
      const qualities = Object.entries(QUALITY).map(([q, t]) => `<option value="${q}"${String(s.quality || 1) === q ? " selected" : ""}>${esc(t)}</option>`).join("");
      const count = key === "food" || key === "potion"
        ? `<input type="number" data-count="${key}" min="1" max="999" value="${s.count || 1}" title="Количество">` : "";
      return `<div class="build-slot"><span class="field-label">${esc(title)}</span>
        <input type="text" data-slot="${key}" value="${esc(s.item || "")}" placeholder="предмет">
        <select data-quality="${key}">${qualities}</select>${count}</div>`;
    }).join("");
    $$("input[data-slot]", box).forEach((i) => itemPicker(i, { slot: i.dataset.slot }));
    box.onchange = () => this.saveDraft(el);
  },
  saveDraft(el) { saveSettings({ ...this.read(el), id: this.id }, BUILD_DRAFT); },
  read(el) {
    const slots = {};
    for (const [key] of this.slots) {
      const item = $(`input[data-slot="${key}"]`, el)?.value.trim();
      if (!item) continue;
      slots[key] = { item, quality: Number($(`select[data-quality="${key}"]`, el).value) };
      const c = $(`input[data-count="${key}"]`, el);
      if (c) slots[key].count = Number(c.value) || 1;
    }
    return { name: $("#build-name", el).value.trim(), slots };
  },
  async calc(el) {
    const build = this.read(el);
    this.saveDraft(el);
    const sum = $("#build-summary", el);
    if (!Object.keys(build.slots).length) { sum.textContent = "Добавьте предметы в слоты."; return; }
    sum.textContent = "Считаю…";
    try {
      const p = this.form.read();
      const r = await apiPost("/api/build-price", { build, markets: p.markets, max_age: p.max_age });
      sum.innerHTML = r.complete ? "" : `<span class="warn">Не для всех предметов есть цены — откройте их на рынке в игре.</span>`;
      const best = r.markets.find((m) => m.complete);
      $("#build-cards", el).innerHTML = [
        card(fmt(r.cheapest_total), "дешевле всего (по слотам)"),
        card(best ? fmt(best.total) : "—", best ? `целиком в: ${App.locName(best.market)}` : "нигде нет всего"),
        card(fmt(r.average_ip), "средняя сила"),
      ].join("");
      this.rowsTable.set(r.rows);
      this.marketTable.set(r.markets);
    } catch (e) { summaryLine(sum, "", e.message); }
  },
  async save(el) {
    try {
      const r = await apiPost("/api/builds", { action: "save", build: this.read(el), id: this.id });
      this.id = r.id; this.saved = r.builds; this.renderList(el); this.saveDraft(el);
      summaryLine($("#build-summary", el), "Сохранено.");
    } catch (e) { summaryLine($("#build-summary", el), "", e.message); }
  },
  async remove(el) {
    if (!this.id) return;
    const r = await apiPost("/api/builds", { action: "delete", id: this.id });
    this.id = null; this.saved = r.builds; this.renderList(el); this.saveDraft(el);
  },
});

// ---------- Мета по киллборду ----------
App.tab({
  id: "meta", group: "tools", title: "Мета", live: true,
  init(el) {
    el.innerHTML = `<p class="muted intro">Популярные билды и предметы по свежим убийствам из официального киллборда игры. Загрузка идёт с серверов Albion Online, только если вы её включили; хранится 7 дней.</p>
      <div class="buttons kb-bar">
        <select id="kb-region" title="Сервер игры"></select>
        <label class="inline"><input type="checkbox" id="kb-enabled"> Загружать киллборд (раз в 5 минут)</label>
        <button type="button" class="secondary" id="kb-fetch">Обновить сейчас</button>
      </div>
      <div class="summary" id="kb-status"></div>`;
    this.form = toolForm(el, "albion-trader-meta", [{ legend: "Фильтры", fields: [
      { type: "select", name: "hours", label: "Период", value: "24",
        options: [["3", "3 часа"], ["6", "6 часов"], ["24", "сутки"], ["72", "3 дня"], ["168", "неделя"]] },
      { type: "number", name: "min_ip", label: "Мин. средняя сила", value: 0, min: 0, step: 50 },
      { type: "select", name: "mode", label: "Бои", options: [["all", "все"], ["solo", "соло (1 участник)"], ["group", "группой"]] },
    ] }], { submitText: "Показать", onSubmit: () => this.refresh(el) });
    el.insertAdjacentHTML("beforeend", `<h2>Популярные билды</h2><div id="kb-builds"></div>
      <h2>Популярные предметы</h2><div id="kb-popular" class="kb-popular"></div>
      <h2>Спрос на замену</h2><p class="muted intro">Что чаще всего теряют в боях — эти предметы постоянно покупают заново. Оборот = потеряно × текущая цена.</p><div id="kb-demand"></div>`);
    const names = (r) => ["mainhand", "offhand"].map((s) => r.names[s]).filter(Boolean).join(" + ");
    this.builds = makeTable($("#kb-builds", el), [
      { key: "weapon", title: "Билд", sort: (r) => names(r),
        html: (r) => `<b>${esc(names(r))}</b><span class="sub">${["armor", "head", "shoes"].map((x) => esc(r.names[x] || "—")).join(" · ")}</span>` },
      { key: "total", title: "Боёв", num: true, hint: "убийств + смертей этим билдом" },
      { key: "win_rate", title: "Побед", num: true, html: (r) => `<span class="${r.win_rate >= 50 ? "good" : "bad"}">${pct(r.win_rate)}</span>` },
      { key: "avg_ip", title: "Ср. сила", num: true, html: (r) => fmt(r.avg_ip) },
      { key: "act", title: "", html: (r, i) => `<button type="button" class="secondary kb-import" data-i="${r._i}" title="Открыть этот билд в конструкторе с ценами">⇢ билд</button>` },
    ], { sort: "total", empty: "Нет данных — включите загрузку киллборда" });
    this.demand = makeTable($("#kb-demand", el), [
      { key: "name", title: "Предмет", html: (r) => itemCell(r) },
      { key: "lost", title: "Потеряно", num: true },
      { key: "price", title: "Цена", num: true, html: (r) => fmt(r.price) },
      { key: "turnover", title: "Оборот", num: true, html: (r) => fmt(r.turnover) },
    ], { sort: "turnover", empty: "—" });
    $("#kb-builds", el).addEventListener("click", (e) => {
      const b = e.target.closest(".kb-import");
      if (!b) return;
      const row = this.last.builds[Number(b.dataset.i)];
      saveSettings({ name: names(row), slots: row.example }, BUILD_IMPORT);
      App.go("build");
    });
    $("#kb-enabled", el).addEventListener("change", async (e) => { await apiPost("/api/killboard", { enabled: e.target.checked }); setTimeout(() => this.refresh(el), 1500); });
    $("#kb-region", el).addEventListener("change", async (e) => { await apiPost("/api/killboard", { region: e.target.value }); this.refresh(el); });
    $("#kb-fetch", el).addEventListener("click", async () => { await apiPost("/api/killboard", { action: "fetch" }); setTimeout(() => this.refresh(el), 2000); });
    this.refresh(el);
  },
  async refresh(el) {
    try {
      const r = await api("/api/killboard", this.form.read());
      this.last = r;
      r.builds.forEach((b, i) => { b._i = i; });
      const sel = $("#kb-region", el);
      if (!sel.options.length) sel.innerHTML = r.regions.map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join("");
      sel.value = r.region;
      $("#kb-enabled", el).checked = r.enabled;
      const st = r.status;
      $("#kb-status", el).innerHTML = `Сохранено событий: ${fmt(r.stored)}${r.latest ? `, последнее ${age(r.latest, r.now)} назад` : ""}`
        + (st.last_fetch_at ? ` · загрузка ${age(st.last_fetch_at, r.now)} назад (+${fmt(st.last_new)})` : "")
        + (st.running ? " · загружаю…" : "") + (st.error ? ` · <span class="bad">ошибка: ${esc(st.error)}</span>` : "")
        + ` · в выборке боёв: ${fmt(r.events)}`;
      this.builds.set(r.builds);
      this.demand.set(r.demand);
      const slotNames = { mainhand: "Оружие", armor: "Броня", head: "Голова", shoes: "Обувь", offhand: "Вторая рука", cape: "Плащ" };
      $("#kb-popular", el).innerHTML = Object.entries(slotNames).filter(([s]) => r.popular[s]).map(([s, t]) =>
        `<div class="card"><div class="l">${esc(t)}</div><ol>${r.popular[s].slice(0, 5).map((i) => `<li>${esc(i.name)} <span class="muted">× ${fmt(i.count)}</span></li>`).join("")}</ol></div>`).join("");
    } catch (e) { summaryLine($("#kb-status", el), "", e.message); }
  },
});

// ---------- Доска судьбы ----------
const DESTINY_OPEN = "albion-trader-destiny-open";

function hoursText(h) {
  if (h === null || h === undefined) return "—";
  if (h === 0) return "готово";
  return h < 48 ? `${fmt1(h)} ч игры` : `${fmt1(h / 24)} дн. игры`;
}

App.tab({
  id: "destiny", group: "tools", title: "Доска", live: true,
  init(el) {
    el.innerHTML = `<p class="muted intro">Отметьте узлы Доски судьбы, которые качаете: текущий уровень и славу внутри уровня (видно в игре на доске), цель. Слава нужного типа (бой, сбор, крафт, рыбалка), набранная после отметки, прибавляется сама; прогноз — по вашей средней славе в час.</p>
      <div class="buttons"><input type="search" id="ds-q" placeholder="найти узел: палаш, сумка, рыбалка…">
        <label>Прогноз по последним <select id="ds-days"><option value="1">суткам</option><option value="7" selected>7 дням</option><option value="30">30 дням</option></select></label></div>
      <div id="ds-found" class="ds-found"></div>
      <form id="ds-form" class="filters row" hidden>
        <strong id="ds-form-title" class="grow"></strong>
        <label>Текущий уровень <input type="number" name="level" min="0" max="100" value="0"></label>
        <label>Слава в уровне <input type="number" name="progress" min="0" value="0" step="100"></label>
        <label>Цель <input type="number" name="target" min="1" max="100" value="100"></label>
        <label class="inline"><input type="checkbox" name="active" checked> Качаю сейчас (прибавлять славу)</label>
        <button type="submit" class="primary">Отслеживать</button>
      </form>
      <div class="summary" id="ds-summary"></div><div id="ds-rows"></div>`;
    const srcName = (s) => (this.sources || {})[s] || "не отслеживается";
    this.table = makeTable($("#ds-rows", el), [
      { key: "title", title: "Узел", html: (r) => `<b>${esc(r.title)}</b><span class="sub">${esc(srcName(r.source))}${r.active ? "" : " · на паузе"}
        <span class="row-actions"><button type="button" data-edit="${esc(r.node_id)}">изменить</button><button type="button" data-del="${esc(r.node_id)}" title="Не отслеживать">убрать</button></span></span>` },
      { key: "level", title: "Уровень", num: true, html: (r) => `${r.level} → ${r.target}<div class="bar"><span style="width:${r.next_level_fame ? Math.min(100, r.progress / r.next_level_fame * 100) : 100}%"></span></div>` },
      { key: "remaining", title: "Осталось", num: true, hint: "славы до цели; ниже — набрано с момента отметки",
        html: (r) => `${fmt(r.remaining)}<span class="sub">+${fmt(r.gained)}</span>` },
      { key: "eta_hours", title: "Прогноз", num: true, hint: "время игры до цели при вашей средней славе в час",
        html: (r) => `${hoursText(r.eta_hours)}<span class="sub">${r.rate ? `${fmt(r.rate)}/ч` : ""}</span>` },
    ], { sort: "eta_hours", asc: true, empty: "Пока ничего не отслеживается — найдите узел выше" });
    $("#ds-q", el).addEventListener("input", debounce(() => this.search(el), 250));
    $("#ds-days", el).addEventListener("change", () => this.refresh(el));
    $("#ds-found", el).addEventListener("click", (e) => {
      const b = e.target.closest("[data-node]");
      if (b) this.edit(el, b.dataset.node, b.dataset.title);
    });
    $("#ds-rows", el).addEventListener("click", async (e) => {
      const ed = e.target.closest("[data-edit]"), del = e.target.closest("[data-del]");
      if (ed) {
        const r = this.rows.find((x) => x.node_id === ed.dataset.edit);
        this.edit(el, r.node_id, r.title, r);
      } else if (del) {
        this.apply(el, await apiPost("/api/destiny", { action: "untrack", node: del.dataset.del }));
      }
    });
    $("#ds-form", el).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        this.apply(el, await apiPost("/api/destiny", { action: "track", node: this.editing, level: f.level.value,
          progress: f.progress.value, target: f.target.value, active: f.active.checked }));
        f.hidden = true;
      } catch (err) { summaryLine($("#ds-summary", el), "", err.message); }
    });
    this.refresh(el);
  },
  show(el) {
    const open = loadSettings(DESTINY_OPEN);
    if (open.node) { saveSettings({}, DESTINY_OPEN); this.edit(el, open.node, open.title || open.node); }
  },
  edit(el, node, title, row) {
    this.editing = node;
    const f = $("#ds-form", el);
    f.hidden = false;
    $("#ds-form-title", el).textContent = title;
    f.level.value = row ? row.level : 0;
    f.progress.value = row ? row.progress : 0;
    f.target.value = row ? row.target : 100;
    f.active.checked = row ? row.active : true;
    f.scrollIntoView({ block: "nearest" });
  },
  async search(el) {
    const q = $("#ds-q", el).value.trim();
    const box = $("#ds-found", el);
    if (q.length < 2) { box.innerHTML = ""; return; }
    const r = await api("/api/destiny", { q, limit: 12, days: $("#ds-days", el).value });
    box.innerHTML = r.nodes.length ? r.nodes.map((n) => `<button type="button" class="secondary" data-node="${esc(n.node_id)}" data-title="${esc(n.title)}" title="До 100 уровня: ${fmt(n.total_fame)} славы">${esc(n.title)}</button>`).join("")
      : `<span class="muted">${r.has_data ? "Ничего не найдено" : "Нет данных Доски судьбы — выполните update-items"}</span>`;
  },
  async refresh(el) {
    try { this.apply(el, await api("/api/destiny", { days: $("#ds-days", el).value })); } catch (e) { summaryLine($("#ds-summary", el), "", e.message); }
  },
  apply(el, r) {
    this.sources = r.sources;
    this.rows = r.rows;
    const rates = Object.entries(r.rates).filter(([, v]) => v).map(([k, v]) => `${esc(r.sources[k])}: ${fmt(v)}`);
    $("#ds-summary", el).innerHTML = r.has_data === false ? `<span class="warn">Нет данных Доски судьбы — выполните update-items.</span>`
      : `Слава в час за период (${fmt1(r.hours)} ч в игре): ${rates.length ? rates.join(" · ") : "пока нет данных"}`;
    this.table.set(r.rows);
  },
});

// ---------- Картограф Дорог Авалона ----------
function leftText(sec) {
  if (sec === null || sec === undefined) return "неизвестно";
  if (sec <= 0) return "закрылся";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h} ч ${m} мин` : `${m} мин`;
}
function zoneLabel(z) {
  return `<span class="${z.road ? "road" : ""}">${esc(z.name || z.id)}</span>`;
}
function zonePicker(input) {
  const id = `dlz-${Math.random().toString(36).slice(2)}`;
  const list = document.createElement("datalist");
  list.id = id;
  input.setAttribute("list", id);
  input.after(list);
  input.addEventListener("input", debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    try {
      const r = await api("/api/avalon", { q, limit: 30 });
      list.innerHTML = r.zones.map((z) => `<option value="${esc(z.name)}">${z.road ? "Дорога Авалона" : esc(z.type)}</option>`).join("");
    } catch { /* подсказки необязательны */ }
  }, 250));
}

App.tab({
  id: "avalon", group: "tools", title: "Авалон", live: true,
  init(el) {
    el.innerHTML = `<p class="muted intro">Порталы Дорог Авалона: переход через портал запоминается сам (время жизни укажите вручную — в игре оно видно на портале), связи можно добавить и руками. Маршрут ищется по связям и обычным переходам карты.</p>
      <div class="summary" id="av-zone"></div>
      <form id="av-route" class="filters row">
        <label class="grow">Откуда <input type="text" name="from" placeholder="текущая зона"></label>
        <label class="grow">Куда <input type="text" name="to" placeholder="зона или город"></label>
        <label class="inline"><input type="checkbox" name="static" checked> обычные переходы карты</label>
        <button type="submit" class="primary">Маршрут</button>
      </form>
      <div id="av-route-out"></div>
      <details class="tool-form" id="av-add-box"><summary>Добавить связь</summary>
        <form id="av-add" class="filters row">
          <label class="grow">Зона A <input type="text" name="a" placeholder="например, Ouyos-Aoeuam"></label>
          <label class="grow">Зона B <input type="text" name="b"></label>
          <label>Портал <select name="size"><option value="0">?</option><option value="2">на 2</option><option value="7">на 7</option><option value="20">на 20</option></select></label>
          <label>Осталось, ч <input type="number" name="hours" min="0" step="1" value="0"></label>
          <label>мин <input type="number" name="minutes" min="0" max="59" step="1" value="0"></label>
          <label class="grow">Заметка <input type="text" name="note" maxlength="200"></label>
          <button type="submit" class="primary">Добавить</button>
        </form>
      </details>
      <h2>Связи</h2><div id="av-links"></div>`;
    ["from", "to"].forEach((n) => zonePicker($(`#av-route [name=${n}]`, el)));
    ["a", "b"].forEach((n) => zonePicker($(`#av-add [name=${n}]`, el)));
    this.table = makeTable($("#av-links", el), [
      { key: "a", title: "Связь", sort: (r) => r.a_info.name,
        html: (r) => `${zoneLabel(r.a_info)} ↔ ${zoneLabel(r.b_info)}<span class="sub">${r.source === "auto" ? "замечен при переходе" : "добавлен вручную"}${r.note ? ` · ${esc(r.note)}` : ""}
          <span class="row-actions"><button type="button" data-time="${r.id}">время</button><button type="button" data-del="${r.id}">убрать</button></span></span>` },
      { key: "size", title: "Портал", num: true, html: (r) => r.size ? `на ${r.size}` : "?" },
      { key: "left", title: "Осталось", num: true, sort: (r) => r.left ?? 1e12,
        html: (r) => `<span class="${r.left !== null && r.left < 1800 ? "warn" : ""}">${leftText(r.left)}</span>` },
    ], { sort: "left", asc: true, empty: "Связей нет — пройдите через портал или добавьте вручную" });
    $("#av-route", el).addEventListener("submit", (e) => { e.preventDefault(); this.refresh(el); });
    $("#av-add", el).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        this.apply(el, await apiPost("/api/avalon", { action: "add", a: f.a.value, b: f.b.value, size: f.size.value,
          hours: f.hours.value, minutes: f.minutes.value, note: f.note.value }));
        f.reset();
      } catch (err) { summaryLine($("#av-zone", el), "", err.message); }
    });
    $("#av-links", el).addEventListener("click", async (e) => {
      const t = e.target.closest("[data-time]"), d = e.target.closest("[data-del]");
      try {
        if (d) this.apply(el, await apiPost("/api/avalon", { action: "delete", id: Number(d.dataset.del) }));
        if (t) {
          const row = this.rows.find((r) => r.id === Number(t.dataset.time));
          const ans = prompt("Сколько осталось порталу (часы:минуты), например 3:20", "");
          if (ans === null) return;
          const [h, m] = ans.split(":").map((x) => Number(x) || 0);
          this.apply(el, await apiPost("/api/avalon", { action: "update", id: row.id, size: row.size, hours: h, minutes: m, note: row.note }));
        }
      } catch (err) { summaryLine($("#av-zone", el), "", err.message); }
    });
    this.refresh(el);
  },
  async refresh(el) {
    const f = $("#av-route", el);
    const params = f.to.value.trim() ? { from: f.from.value.trim(), to: f.to.value.trim(), static: f.static.checked } : {};
    try { this.apply(el, await api("/api/avalon", params)); } catch (e) { summaryLine($("#av-zone", el), "", e.message); }
  },
  apply(el, r) {
    this.rows = r.links;
    $("#av-zone", el).innerHTML = (r.zone ? `Вы сейчас: ${zoneLabel(r.zone)}` : "Текущая зона пока неизвестна — смените зону в игре.")
      + (r.has_map === false ? ` <span class="warn">Нет карты мира — выполните update-items.</span>` : "");
    const out = $("#av-route-out", el);
    if (r.route) {
      const rt = r.route;
      out.innerHTML = `<div class="card route"><div class="l">Маршрут: ${rt.steps.length} переход(ов), из них порталов ${rt.portals}${rt.expires ? ` · ближайший портал закроется через ${leftText(rt.expires - r.now)}` : ""}</div>
        <ol>${rt.zones.map((z, i) => `<li>${zoneLabel(z)}${i && rt.steps[i - 1].portal ? ` <span class="pill">портал${rt.steps[i - 1].size ? ` на ${rt.steps[i - 1].size}` : ""}</span>` : ""}</li>`).join("")}</ol></div>`;
    } else out.innerHTML = r.route_error ? `<p class="warn">${esc(r.route_error)}</p>` : "";
    this.table.set(r.links);
  },
});

// ---------- Журнал данжей и сундуков ----------
const RARITY_CLS = { 0: "r0", 1: "r1", 2: "r2", 3: "r3" };
function chestPills(chests, names) {
  const keys = Object.keys(chests || {}).sort((a, b) => Number(b) - Number(a));
  return keys.length ? `<span class="chests">${keys.map((k) => `<span class="chest ${RARITY_CLS[k] || ""}" title="${esc(names[k] || "редкость неизвестна")}">● ${chests[k]}</span>`).join("")}</span>` : "";
}
function duration(sec) {
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
}

App.tab({
  id: "dungeons", group: "tools", title: "Данжи", live: true,
  init(el) {
    el.innerHTML = `<p class="muted intro">Каждое прохождение — от входа в данж до выхода в обычную зону (уровни одного данжа — одно прохождение): время, слава, серебро, ваш лут по рыночной оценке и открытые сундуки по редкости (<span class="chest r0">●</span> обычный, <span class="chest r1">●</span> необычный, <span class="chest r2">●</span> редкий, <span class="chest r3">●</span> легендарный).</p>
      <div class="buttons"><label>Период <select id="dg-days"><option value="1">сутки</option><option value="7" selected>неделя</option><option value="30">30 дней</option></select></label></div>
      <h2>По типам</h2><div id="dg-summary"></div><h2>Прохождения</h2><div id="dg-runs"></div>`;
    this.sum = makeTable($("#dg-summary", el), [
      { key: "name", title: "Тип", html: (r) => `${esc(r.name)}<span class="sub">${chestPills(r.chests, this.rarity) || "сундуков нет"}</span>` },
      { key: "runs", title: "Раз", num: true },
      { key: "avg_minutes", title: "Среднее", num: true, html: (r) => `${fmt1(r.avg_minutes)} мин` },
      { key: "income_per_hour", title: "Доход/ч", num: true, html: (r) => `${fmt(r.income_per_hour)}<span class="sub">слава ${fmt(r.fame_per_hour)}/ч</span>` },
    ], { sort: "income_per_hour", empty: "Прохождений пока нет" });
    this.runs = makeTable($("#dg-runs", el), [
      { key: "started", title: "Когда", html: (r) => `${dateTime(r.started)}<span class="sub">${r.active ? `<span class="good">идёт</span>` : duration(r.seconds)}${r.deaths ? ` · <span class="bad">смертей: ${r.deaths}</span>` : ""}</span>` },
      { key: "kind_name", title: "Данж", html: (r) => `${esc(r.kind_name)}<span class="sub">${esc(r.zone_names.join(" → "))}</span><span class="sub">${chestPills(r.chests, this.rarity)}</span>` },
      { key: "income", title: "Доход", num: true, html: (r) => `${fmt(r.income)}<span class="sub">${fmt(r.income_per_hour)}/ч</span>` },
      { key: "fame", title: "Слава", num: true, html: (r) => `${fmt(r.fame)}<span class="sub">${fmt(r.fame_per_hour)}/ч</span>` },
    ], { sort: "started", empty: "Прохождений пока нет — войдите в данж с запущенным сборщиком" });
    $("#dg-days", el).addEventListener("change", () => this.refresh(el));
    this.refresh(el);
  },
  async refresh(el) {
    try {
      const r = await api("/api/dungeons", { days: $("#dg-days", el).value });
      this.rarity = r.rarity;
      this.sum.set(r.summary);
      this.runs.set(r.runs);
    } catch (e) { $("#dg-runs", el).insertAdjacentHTML("afterbegin", `<p class="bad">${esc(e.message)}</p>`); }
  },
});

// ---------- Точный учёт славы и серебра ----------
App.tab({
  id: "economy", group: "tools", title: "Учёт", live: true,
  init(el) {
    el.innerHTML = `<p class="muted intro">Слава — по полной формуле игры (премиум, сумка прозрения, бонусы) и по источникам. Серебро — только ваше: с мобов после налогов, из лута, рынок; «прочее» — разница с изменением баланса (ремонт, телепорты, комиссии…).</p>
      <div class="buttons"><label>Период <select id="ec-period"><option value="session">текущая сессия</option><option value="today">сегодня</option><option value="7">7 дней</option><option value="30">30 дней</option></select></label></div>
      <div class="cards" id="ec-cards"></div>
      <div class="ec-grid"><div><h2>Слава</h2><div class="table-wrap"><table id="ec-fame"></table></div></div>
        <div><h2>Серебро</h2><div class="table-wrap"><table id="ec-silver"></table></div></div></div>
      <h2 class="ec-per">Серебро по часам</h2><div id="ec-chart-silver"></div>
      <h2 class="ec-per">Слава по часам</h2><div id="ec-chart-fame"></div>
      <h2>По дням</h2><div id="ec-days"></div>`;
    this.days = makeTable($("#ec-days", el), [
      { key: "day", title: "День" }, { key: "fame", title: "Слава", num: true, html: (r) => fmt(r.fame) },
      { key: "silver", title: "Серебро (мобы + лут)", num: true, html: (r) => fmt(r.silver) },
    ], { sort: "day", empty: "—" });
    $("#ec-period", el).addEventListener("change", () => this.refresh(el));
    this.refresh(el);
  },
  async refresh(el) {
    try {
      const r = await api("/api/economy", { period: $("#ec-period", el).value });
      const f = r.fame, s = r.silver, b = r.balance;
      $("#ec-cards", el).innerHTML = [
        card(fmt(r.fame_per_hour), "слава в час"), card(fmt(r.income_per_hour), "доход в час"),
        card(fmt(f.total), "слава всего"), card(fmt(r.income), "доход всего"),
        card(b ? fmt(b.change) : "—", "изменение баланса", b ? (b.change >= 0 ? "good" : "bad") : ""),
        card(`${fmt1(r.hours)} ч`, "в игре"),
      ].join("");
      const row = (label, v, cls = "") => `<tr><td>${label}</td><td class="num ${v ? cls : ""}">${fmt(v || 0)}</td></tr>`;
      $("#ec-fame", el).innerHTML = `<tbody>${Object.entries(f.by_source).map(([k, v]) => row(esc(r.sources[k]), v)).join("")}
        <tr><td colspan="2" class="muted">из чего сложилась</td></tr>
        ${row("база (с множителем зоны)", f.base)}${row("премиум", f.premium)}${row("сумка прозрения", f.satchel)}${row("бонусы", f.bonus)}
        ${row("<b>итого</b>", f.total)}</tbody>`;
      $("#ec-silver", el).innerHTML = `<tbody>${row("с мобов (до налогов)", s.mobs_gross)}${row("налог кластера", -s.cluster_tax, "bad")}
        ${row("налог гильдии", -s.guild_tax, "bad")}${s.alliance_penalty ? row("штраф альянса", -s.alliance_penalty, "bad") : ""}
        ${row("<b>с мобов чистыми</b>", s.mobs_net)}${row("из лута", s.loot)}
        ${row("продажи на рынке (после налога)", r.market.sales_net)}${row("покупки на рынке", -r.market.purchases, "bad")}
        ${b ? `${row("изменение баланса", b.change)}${row("учтено выше", b.explained)}${row("<b>прочее</b> (ремонт, телепорты…)", b.other, b.other < 0 ? "bad" : "good")}`
          : `<tr><td colspan="2" class="muted">баланс пока не виден — смените зону в игре</td></tr>`}</tbody>`;
      const per = r.series_bucket === 86400 ? "по дням" : "по часам";
      $$(".ec-per", el).forEach((h, i) => { h.textContent = `${i ? "Слава" : "Серебро"} ${per}`; });
      lineChart($("#ec-chart-silver", el), [{ name: "серебро (мобы + лут)", slot: 1, points: r.hourly.map((h) => [h.ts, h.silver]) }],
        { height: 180, empty: "Пока нет данных" });
      lineChart($("#ec-chart-fame", el), [{ name: "слава", slot: 3, points: r.hourly.map((h) => [h.ts, h.fame]) }],
        { height: 180, empty: "Пока нет данных" });
      this.days.set(r.daily);
    } catch (e) { $("#ec-cards", el).innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  },
});
