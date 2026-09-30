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
