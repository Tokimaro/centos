"use strict";
// Производство: крафт и переработка, зачарование, дневники, остров.

const NO_GAMEDATA = "Нет игровых таблиц: выполните <code>update-items</code> (или перезапустите start.bat) и обновите страницу.";
const CRAFT_CATEGORIES = [
  ["", "все"], ["weapons", "оружие"], ["head", "шлемы"], ["armors", "броня"], ["shoes", "обувь"],
  ["offhands", "вторая рука"], ["capes", "плащи"], ["bags", "сумки"], ["gathering", "снаряжение собирателя"],
  ["consumables", "еда и зелья"], ["mounts", "ездовые"], ["crafting", "ресурсы"], ["artefacts", "артефакты"],
  ["furniture", "мебель"], ["farming", "фермерство"], ["other", "прочее"],
];
const itemFields = () => [
  { type: "search", name: "q", label: "Поиск", placeholder: "название или ID" },
  { type: "tiers" }, { type: "enchants" },
];
const ageCol = () => ({ key: "seen_at", title: "Возраст", num: true, sort: (r) => -(r.seen_at || 0),
  html: (r) => `<span class="${ageClass(r.seen_at, r._now)}">${age(r.seen_at, r._now)}</span>` });
const profitCol = (key = "profit", title = "Прибыль") => ({ key, title, num: true,
  html: (r) => r[key] === null || r[key] === undefined ? "—" : `<b>${fmt(r[key])}</b>`,
  cls: (r) => (r[key] > 0 ? "good" : r[key] < 0 ? "bad" : "") });

function materialsHint(r) {
  const lines = r.materials.map((m) => `${m.name}: ${m.count} × ${fmt(m.price)}${m.returnable ? "" : " (не возвращается)"}`);
  if (r.missing_names && r.missing_names.length) lines.push(`нет цены: ${r.missing_names.join(", ")}`);
  return lines.join("\n");
}

standardTab({
  id: "craft", group: "prod", title: "Крафт и переработка",
  intro: "Себестоимость по текущим ценам ресурсов против цены продажи. Возврат ресурсов: 1 − 1/(1 + бонус), где бонус — "
    + "18% в королевском городе + специализация города по категории (из игровых таблиц) + 59% за фокус; артефакты и прочие "
    + "невозвращаемые ресурсы не возвращаются. Плата станции = ценность предмета × 0,1125 × ставка / 100. Цена продукта — "
    + "обычного качества. Состав рецепта — во всплывающей подсказке у себестоимости.",
  spec: [
    { legend: "Что считать", fields: [
      { type: "select", name: "kind", label: "Режим", value: "craft", options: [["craft", "крафт"], ["refine", "переработка"], ["transmute", "трансмутация (ресурсы, руны → души…)"]] },
      { type: "select", name: "category", label: "Категория", value: "", options: CRAFT_CATEGORIES },
      { type: "check", name: "focus", label: "С фокусом (+59% к бонусу)", value: false },
      { type: "number", name: "station_fee", label: "Ставка станции (за 100 питания)", value: 400, min: 0 },
      { type: "check", name: "incomplete", label: "Показывать рецепты без всех цен", value: false },
    ] },
    { legend: "Рынки", fields: [
      { type: "market", name: "buy_market", label: "Покупать ресурсы в", buyable: true, value: "martlock" },
      { type: "select", name: "buy_mode", label: "Как покупать", value: "instant", options: [["instant", "мгновенно"], ["order", "заказом"]] },
      { type: "market", name: "craft_city", label: "Крафтить в (бонус)", buyable: true, empty: "там же, где покупаю" },
      { type: "market", name: "sell_market", label: "Продавать в", value: "martlock" },
      { type: "select", name: "sell_mode", label: "Как продавать", value: "instant", options: [["instant", "мгновенно"], ["order", "предложением"]] },
    ] },
    { legend: "Фильтры", fields: [
      { type: "check", name: "premium", label: "Премиум", value: true },
      { type: "number", name: "max_age", label: "Цены не старше, ч", value: 24, min: 0.1 },
      { type: "number", name: "min_margin", label: "Мин. маржа, % (пусто — все)" },
      ...itemFields(),
    ] },
  ],
  columns: [
    { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
    { key: "return_rate", title: "Возврат", num: true, html: (r) => `<span title="бонус ${r.bonus}%">${pct(r.return_rate)}</span>` },
    { key: "cost", title: "Себестоимость", num: true, html: (r) => `<span class="hint" title="${esc(materialsHint(r))}">${fmt(r.cost)}</span>` },
    { key: "station_fee", title: "Станция", num: true, html: (r) => fmt(r.station_fee) },
    { key: "sell_price", title: "Цена продажи", num: true, html: (r) => fmt(r.sell_price) },
    { key: "revenue", title: "На руки", num: true, html: (r) => fmt(r.revenue) },
    profitCol(),
    { key: "margin", title: "Маржа", num: true, html: (r) => pct(r.margin) },
    { key: "daily_volume", title: "Продаж/сут", num: true, html: (r) => fmt(r.daily_volume) },
    { key: "focus", title: "Фокус", num: true, html: (r) => fmt(r.focus), hint: "Базовая стоимость фокуса (без учёта специализации)" },
    ageCol(),
  ],
  sort: "profit",
  empty: "Нет рецептов с ценами. Откройте в игре рынок с ресурсами и готовыми предметами (обе вкладки) или включите «Показывать рецепты без всех цен».",
  async load(f) {
    const data = await api("/api/craft", { ...f, limit: 400 });
    if (data.no_gamedata) return { rows: [], summary: NO_GAMEDATA };
    data.rows.forEach((r) => { r._now = data.now; });
    return { rows: data.rows, summary: `Рецептов: ${data.count}${data.rows.length < data.count ? ` (показано ${data.rows.length})` : ""}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});

// ---------- зачарование ----------
standardTab({
  id: "enchant", group: "prod", title: "Зачарование",
  intro: "Выгодно ли улучшать самому: предмет .a + руны (→ .1), души (→ .2), реликвии (→ .3) — или для еды и зелий соусы "
    + "и экстракты — против продажи готового .b. Качество при зачаровании сохраняется. «Экономия» — насколько дешевле "
    + "зачаровать, чем купить готовый предмет на рынке покупки.",
  spec: [
    { legend: "Рынки", fields: [
      { type: "market", name: "buy_market", label: "Покупать предмет и материалы в", buyable: true, value: "martlock" },
      { type: "select", name: "buy_mode", label: "Как покупать", value: "instant", options: [["instant", "мгновенно"], ["order", "заказом"]] },
      { type: "market", name: "sell_market", label: "Продавать в", value: "martlock" },
      { type: "select", name: "sell_mode", label: "Как продавать", value: "instant", options: [["instant", "мгновенно"], ["order", "предложением"]] },
    ] },
    { legend: "Фильтры", fields: [
      { type: "check", name: "premium", label: "Премиум", value: true },
      { type: "number", name: "max_age", label: "Цены не старше, ч", value: 24, min: 0.1 },
      { type: "number", name: "min_profit", label: "Мин. прибыль (пусто — все)" },
      { type: "qualities" },
      { type: "search", name: "q", label: "Поиск", placeholder: "название или ID" },
      { type: "tiers" },
    ] },
  ],
  columns: [
    { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
    { key: "to_level", title: "Улучшение", html: (r) => `.${r.from_level} → .${r.to_level}`, sort: (r) => r.from_level * 10 + r.to_level },
    { key: "quality", title: "Кач.", html: (r) => `<span title="${QUALITY[r.quality] || ""}">${r.quality}</span>` },
    { key: "start_price", title: "Цена .a", num: true, html: (r) => fmt(r.start_price) },
    { key: "cost", title: "Итого затраты", num: true,
      html: (r) => `<span class="hint" title="${esc(r.materials.map((m) => `${m.name}: ${m.count} × ${fmt(m.price)}`).join("\n"))}">${fmt(r.cost)}</span>` },
    { key: "sell_price", title: "Цена .b", num: true, html: (r) => fmt(r.sell_price) },
    { key: "revenue", title: "На руки", num: true, html: (r) => fmt(r.revenue) },
    profitCol(),
    { key: "margin", title: "Маржа", num: true, html: (r) => pct(r.margin) },
    { key: "saving", title: "Экономия", num: true, html: (r) => fmt(r.saving), hint: "Готовый .b на рынке покупки минус затраты на зачарование" },
    ageCol(),
  ],
  sort: "profit",
  empty: "Нет вариантов: нужны цены базового предмета, материалов (руны/души/реликвии) и зачарованного предмета.",
  async load(f) {
    const data = await api("/api/enchant", { ...f, limit: 400 });
    if (data.no_gamedata) return { rows: [], summary: NO_GAMEDATA };
    data.rows.forEach((r) => { r._now = data.now; });
    return { rows: data.rows, summary: `Вариантов: ${data.count}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});

// ---------- дневники ----------
standardTab({
  id: "journals", group: "prod", title: "Дневники",
  intro: "Выгода заполнения дневника (продать полный − купить пустой) и ожидаемая добыча работника: базовый объём × "
    + "средняя выручка за единицу по весам таблицы добычи × счастье работника. «Покрытие» — доля таблицы добычи, для "
    + "которой известны цены. «Работник выгоднее на» — добыча работника минус выручка за полный дневник.",
  spec: [
    { legend: "Рынки", fields: [
      { type: "market", name: "buy_market", label: "Покупать пустые в", buyable: true, value: "martlock" },
      { type: "market", name: "sell_market", label: "Продавать полные и добычу в", value: "martlock" },
    ] },
    { legend: "Параметры", fields: [
      { type: "number", name: "happiness", label: "Счастье работника, %", value: 100, min: 50, step: 1 },
      { type: "check", name: "premium", label: "Премиум", value: true },
      { type: "number", name: "max_age", label: "Цены не старше, ч", value: 24, min: 0.1 },
      { type: "search", name: "q", label: "Поиск", placeholder: "например, WOOD или Лесоруб" },
      { type: "tiers" },
    ] },
  ],
  columns: [
    { key: "name", title: "Дневник", html: itemCell, sort: (r) => r.name },
    { key: "empty_price", title: "Пустой", num: true, html: (r) => fmt(r.empty_price) },
    { key: "full_price", title: "Полный", num: true, html: (r) => fmt(r.full_price) },
    profitCol("fill_profit", "Выгода заполнения"),
    { key: "fame", title: "Слава", num: true, html: (r) => fmt(r.fame) },
    { key: "laborer_value", title: "Добыча работника", num: true, html: (r) => fmt(r.laborer_value) },
    { key: "coverage", title: "Покрытие", num: true, html: (r) => pct(r.coverage) },
    profitCol("laborer_vs_sell", "Работник выгоднее на"),
    ageCol(),
  ],
  sort: "fill_profit",
  empty: "Нет цен дневников. Откройте в игре рынок с дневниками (категория «Прочее → Работники»).",
  async load(f) {
    const data = await api("/api/journals", { ...f, limit: 400 });
    if (data.no_gamedata) return { rows: [], summary: NO_GAMEDATA };
    data.rows.forEach((r) => { r._now = data.now; });
    return { rows: data.rows, summary: `Дневников: ${data.count}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});

// ---------- остров ----------
standardTab({
  id: "farming", group: "prod", title: "Остров",
  intro: "Упрощённая модель доходности фермы. Растения — за один цикл роста: ожидаемый урожай по таблице (шанс × средний "
    + "объём) × цена продукта минус стоимость семени с учётом шанса его возврата (с поливом/фокусом шанс выше). Животные — "
    + "за выращивание: выросшее животное + шанс потомства × детёныш − детёныш − корм (введите стоимость корма на одно "
    + "животное). Если детёныша/семени нет на рынке, берётся цена фермера-торговца.",
  spec: [
    { legend: "Рынки", fields: [
      { type: "market", name: "buy_market", label: "Покупать семена и детёнышей в", buyable: true, value: "martlock" },
      { type: "market", name: "sell_market", label: "Продавать урожай в", value: "martlock" },
    ] },
    { legend: "Параметры", fields: [
      { type: "select", name: "kind", label: "Показывать", value: "", options: [["", "всё"], ["plant", "растения"], ["animal", "животных"]] },
      { type: "check", name: "focus", label: "С поливом / фокусом", value: false },
      { type: "number", name: "yield_mult", label: "Множитель урожая, %", value: 100, min: 0, step: 1, title: "Например, 200 — если премиум удваивает урожай" },
      { type: "number", name: "feed_cost", label: "Корм на одно животное, серебро", value: 0, min: 0 },
      { type: "check", name: "premium", label: "Премиум (налог)", value: true },
      { type: "number", name: "max_age", label: "Цены не старше, ч", value: 24, min: 0.1 },
      { type: "tiers" },
    ] },
  ],
  columns: [
    { key: "name", title: "Семя / детёныш", html: itemCell, sort: (r) => r.name },
    { key: "product_name", title: "Продукт", html: (r) => esc(r.product_name) },
    { key: "input_price", title: "Цена", num: true, html: (r) => `${fmt(r.input_price)}${r.input_source === "торговец" ? ' <span class="muted" title="цена фермера-торговца">т</span>' : ""}` },
    { key: "return_chance", title: "Возврат / потомство", num: true, html: (r) => pct(r.return_chance) },
    { key: "revenue", title: "Выручка", num: true,
      html: (r) => r.parts.length ? `<span class="hint" title="${esc(r.parts.map((x) => `${x.name}: ${x.chance * 100}% × ${x.amount} × ${fmt(x.price)}`).join("\n"))}">${fmt(r.revenue)}</span>` : fmt(r.revenue) },
    { key: "cost", title: "Затраты", num: true, html: (r) => fmt(r.cost) },
    profitCol(),
    { key: "cycle_hours", title: "Цикл, ч", num: true, html: (r) => fmt1(r.cycle_hours) },
    profitCol("profit_per_day", "Прибыль в сутки"),
    { key: "fame", title: "Слава", num: true, html: (r) => fmt(r.fame) },
  ],
  sort: "profit_per_day",
  empty: "Нет цен урожая. Откройте в игре рынок с продуктами фермы (категория «Фермерство»).",
  async load(f) {
    const data = await api("/api/farming", { ...f, limit: 400 });
    if (data.no_gamedata) return { rows: [], summary: NO_GAMEDATA };
    return { rows: data.rows, summary: `Позиций: ${data.count}. Обновлено ${new Date().toLocaleTimeString("ru-RU")}.` };
  },
});
