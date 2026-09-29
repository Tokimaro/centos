"use strict";
// Мои данные: сделки и заказы, сессия, лут, персонаж.

const EXPERIMENTAL_HINT = "Определено по формату, подтверждённому только исходниками открытых инструментов; сверяйте с игрой.";

App.tab({
  id: "mytrades", group: "my", title: "Сделки",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Всё собирается автоматически, когда вы сами открываете окна игры: вкладку своих заказов на рынке,
        вкладку завершённых сделок, почту с итогами заказов; мгновенные покупки и продажи записываются в момент сделки.
        Выручка продаж — после налога; прибыль — по средней цене ваших покупок этого предмета. Строки со значком
        <span class="pill warn">β</span> определены экспериментально.</p>
      <form class="chart-controls" id="mt-form"><label>Период <select name="days">
        <option value="1">сутки</option><option value="7">неделя</option><option value="30" selected>месяц</option>
        <option value="365">год</option></select></label></form>
      <div class="cards" id="mt-cards"></div>
      <h2>Мои открытые заказы</h2><div id="mt-orders"></div>
      <h2>Прибыль по предметам</h2><div id="mt-items"></div>
      <h2>По дням</h2><div id="mt-days"></div>
      <h2>Журнал сделок</h2><div id="mt-trades"></div>`;
    this.orders = makeTable($("#mt-orders"), [
      { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
      { key: "auction_type", title: "Тип", html: (r) => (r.auction_type === "offer" ? "продажа" : "покупка") },
      { key: "location", title: "Рынок", html: (r) => (r.location ? locCell(r.location) : "—") },
      { key: "quality", title: "Кач.", num: true },
      { key: "price", title: "Цена", num: true, html: (r) => fmt(r.price) },
      { key: "amount", title: "Шт.", num: true, html: (r) => fmt(r.amount) },
      { key: "status", title: "Статус", html: (r) => orderStatus(r), sort: (r) => (r.outbid ? 1 : 0) },
      { key: "expires", title: "Истекает", num: true, html: (r) => (r.expires ? dateTime(r.expires) : "—") },
      { key: "seen_at", title: "Обновлено", num: true, html: (r) => `<span class="${ageClass(r.seen_at, r._now)}">${age(r.seen_at, r._now)}</span>` },
    ], { empty: "Откройте в игре на рынке вкладку своих заказов — список появится здесь." });
    this.items = makeTable($("#mt-items"), [
      { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
      { key: "bought", title: "Куплено", num: true, html: (r) => fmt(r.bought) },
      { key: "avg_cost", title: "Средняя цена", num: true, html: (r) => fmt(r.avg_cost) },
      { key: "sold", title: "Продано", num: true, html: (r) => fmt(r.sold) },
      { key: "revenue", title: "Выручка", num: true, html: (r) => fmt(r.revenue) },
      { key: "profit", title: "Прибыль", num: true, html: (r) => (r.profit === null ? "—" : `<b>${fmt(r.profit)}</b>`), cls: (r) => (r.profit > 0 ? "good" : r.profit < 0 ? "bad" : "") },
      { key: "stock", title: "На руках", num: true, html: (r) => fmt(r.stock) },
    ], { sort: "profit", empty: "Сделок за период нет." });
    this.days = makeTable($("#mt-days"), [
      { key: "day", title: "День" },
      { key: "bought", title: "Потрачено", num: true, html: (r) => fmt(r.bought) },
      { key: "sold", title: "Выручка", num: true, html: (r) => fmt(r.sold) },
      { key: "profit", title: "Прибыль", num: true, html: (r) => `<b>${fmt(r.profit)}</b>`, cls: (r) => (r.profit > 0 ? "good" : r.profit < 0 ? "bad" : "") },
    ], { sort: "day", empty: "—" });
    this.trades = makeTable($("#mt-trades"), [
      { key: "ts", title: "Когда", html: (r) => dateTime(r.ts) },
      { key: "kind", title: "Операция", html: (r) => `${r.kind === "buy" ? "покупка" : "продажа"}${r.experimental ? ` <span class="pill warn" title="${EXPERIMENTAL_HINT}">β</span>` : ""}` },
      { key: "source", title: "Как", html: (r) => ({ instant: "мгновенно", order: "заказ", mail: "почта" }[r.source] || r.source) },
      { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
      { key: "location", title: "Рынок", html: (r) => (r.location ? locCell(r.location) : "—") },
      { key: "amount", title: "Шт.", num: true, html: (r) => fmt(r.amount) },
      { key: "unit_price", title: "Цена", num: true, html: (r) => fmt(r.unit_price) },
      { key: "total", title: "Сумма", num: true, html: (r) => fmt(r.total) },
    ], { sort: "ts", empty: "Сделок за период нет." });
    $("#mt-form").addEventListener("change", () => this.reload());
    this.reload = async () => {
      const days = $("#mt-form").days.value;
      const [orders, trades] = await Promise.all([api("/api/my/orders"), api("/api/my/trades", { days })]);
      orders.rows.forEach((r) => { r._now = orders.now; });
      this.orders.set(orders.rows);
      this.items.set(trades.items);
      this.days.set(trades.days);
      this.trades.set(trades.trades);
      const sum = (arr, k) => arr.reduce((a, r) => a + (r[k] || 0), 0);
      const today = trades.days.length ? trades.days[0] : { profit: 0 };
      $("#mt-cards").innerHTML = `
        <div class="card"><div class="v">${fmt(sum(trades.days, "profit"))}</div><div class="l">прибыль за период</div></div>
        <div class="card"><div class="v">${fmt(today.profit)}</div><div class="l">прибыль за ${esc(today.day || "сегодня")}</div></div>
        <div class="card"><div class="v">${fmt(sum(trades.days, "sold"))}</div><div class="l">выручка за период</div></div>
        <div class="card"><div class="v">${fmt(sum(trades.days, "bought"))}</div><div class="l">потрачено за период</div></div>
        <div class="card"><div class="v">${fmt(orders.rows.length)}</div><div class="l">открытых заказов${orders.character ? ` · ${esc(orders.character)}` : ""}</div></div>`;
    };
    this.reload();
  },
  show() { if (this.reload) this.reload(); },
});

function orderStatus(r) {
  if (r.outbid === undefined) return "—";
  if (!r.outbid) return '<span class="pill good">лучший</span>';
  return `<span class="pill bad">перебит</span> <span class="muted">лучший ${fmt(r.best_price)} → ставьте ${fmt(r.suggested_price)}</span>`;
}

// ---------- сессия ----------
function duration(hours) {
  const m = Math.round(hours * 60);
  return m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

App.tab({
  id: "session", group: "my", title: "Сессия",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Серебро, слава и лут за игровую сессию. Сессия начинается при запуске приложения или по кнопке.
        Лут оценивается по медиане лучших цен продажи в городах за 48 часов (иначе — по средней цене сделок); учитывается
        только то, что подобрал ваш персонаж. Данные идут из событий игры — ничего нажимать не нужно.</p>
      <div class="buttons">
        <label class="rule-kind">Сессия <select id="session-select"></select></label>
        <button type="button" class="secondary" id="session-new">Начать новую сессию</button>
      </div>
      <div class="cards" id="session-cards"></div>
      <h2>Лут</h2><div id="session-items"></div>
      <h2>По зонам</h2><div id="session-zones"></div>`;
    this.items = makeTable($("#session-items"), [
      { key: "name", title: "Предмет", html: itemCell, sort: (r) => r.name },
      { key: "amount", title: "Шт.", num: true, html: (r) => fmt(r.amount) },
      { key: "unit_value", title: "Цена", num: true, html: (r) => fmt(r.unit_value) },
      { key: "value", title: "Стоимость", num: true, html: (r) => `<b>${fmt(r.value)}</b>` },
    ], { sort: "value", empty: "Лута пока нет." });
    this.zones = makeTable($("#session-zones"), [
      { key: "location", title: "Зона", html: (r) => esc(r.name || r.location), sort: (r) => r.name || r.location },
      { key: "first", title: "С", html: (r) => dateTime(r.first) },
      { key: "last", title: "По", html: (r) => dateTime(r.last) },
      { key: "fame", title: "Слава", num: true, html: (r) => fmt(r.fame) },
      { key: "silver", title: "Серебро", num: true, html: (r) => fmt(r.silver) },
    ], { empty: "—" });
    $("#session-new").addEventListener("click", async () => {
      await apiPost("/api/session/new", {});
      $("#session-select").value = "";
      this.reload();
    });
    $("#session-select").addEventListener("change", () => this.reload());
    this.reload = async () => {
      const sel = $("#session-select");
      const data = await api("/api/session", { id: sel.value });
      const keep = sel.value;
      sel.innerHTML = `<option value="">текущая</option>` + data.sessions.map((s) =>
        `<option value="${s.id}">${dateTime(s.started)}${s.ended ? ` — ${dateTime(s.ended)}` : " (идёт)"}</option>`).join("");
      sel.value = keep;
      const r = data.report;
      if (!r || !r.session) { $("#session-cards").innerHTML = ""; return; }
      const card = (v, l) => `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`;
      $("#session-cards").innerHTML = [
        card(duration(r.hours), `длительность${r.character ? ` · ${esc(r.character)}` : ""}`),
        card(fmt(r.fame_per_hour), `славы в час (всего ${fmt(r.fame)})`),
        card(fmt(r.silver_per_hour), `серебра в час (всего ${fmt(r.silver)})`),
        card(fmt(r.loot_value_per_hour), `лута в час, по рынку (всего ${fmt(r.loot_value)})`),
        card(r.balance_change === null ? "—" : fmt(r.balance_change), "изменение баланса серебра"),
        card(`${r.kills} / ${r.deaths}`, "убийств / смертей"),
      ].join("");
      this.items.set(r.items);
      this.zones.set(r.zones);
    };
    this.reload();
    this.timer = setInterval(() => { if (App.current === App.byId("session") && !$("#session-select").value) this.reload(); }, 15000);
  },
  show() { if (this.reload) this.reload(); },
});
