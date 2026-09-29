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
