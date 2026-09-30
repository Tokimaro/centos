"use strict";
// Инструменты: работают и в основном интерфейсе (группа «Инструменты»),
// и в окне-компаньоне (companion.html загружает только core.js и этот файл).

// Кнопка «Отдельное окно»: на этом компьютере сервер открывает окно-приложение
// (можно закрепить поверх игры); с другого устройства — всплывающее окно браузера.
async function openCompanion() {
  try {
    const r = await apiPost("/api/window", { action: "open" });
    if (r.mode) return;
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
      { key: "name", title: "Предмет", html: (r) => `${itemCell(r)}${r.spec_name ? `<span class="sub">${esc(r.spec_name)}</span>` : ""}` },
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
