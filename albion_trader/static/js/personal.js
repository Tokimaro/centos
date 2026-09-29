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

// ---------- журнал лута ----------
App.tab({
  id: "loot", group: "my", title: "Лут и бои",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Кто что подобрал рядом с вами — полезно для честного дележа в группе. Стоимость предметов —
        по медиане лучших цен продажи в городах (иначе по средней цене сделок).</p>
      <form class="chart-controls" id="loot-form" autocomplete="off">
        <label>Период <select name="period">
          <option value="session">текущая сессия</option><option value="1">сутки</option>
          <option value="7" selected>неделя</option><option value="30">месяц</option></select></label>
        <label>Игрок <input type="search" name="player" placeholder="имя"></label>
      </form>
      <h2>Итоги по игрокам</h2><div id="loot-players"></div>
      <h2>Журнал</h2><div id="loot-rows"></div>
      <h2>Убийства и смерти</h2>
      <p class="muted">Смерти, которые игра показала рядом с вами, и ваши убийства. Фильтр «Игрок» работает и здесь.</p>
      <div class="cards" id="kill-cards"></div>
      <div id="kill-top"></div>
      <div id="kill-rows"></div>`;
    this.players = makeTable($("#loot-players"), [
      { key: "player", title: "Игрок", html: (r) => `${esc(r.player)}${r.player === this.me ? ' <span class="pill good">вы</span>' : ""}` },
      { key: "items", title: "Предметов", num: true, html: (r) => fmt(r.items) },
      { key: "value", title: "Стоимость предметов", num: true, html: (r) => fmt(r.value) },
      { key: "silver", title: "Серебро", num: true, html: (r) => fmt(r.silver) },
      { key: "total", title: "Итого", num: true, html: (r) => `<b>${fmt(r.value + r.silver)}</b>`, sort: (r) => r.value + r.silver },
    ], { sort: "total", empty: "Лута пока не было." });
    this.rows = makeTable($("#loot-rows"), [
      { key: "ts", title: "Когда", html: (r) => dateTime(r.ts) },
      { key: "actor", title: "Кто", html: (r) => esc(r.actor || "?") },
      { key: "name", title: "Что", html: (r) => (r.silver ? "серебро" : itemCell(r)), sort: (r) => r.name },
      { key: "amount", title: "Шт.", num: true, html: (r) => fmt(r.amount) },
      { key: "value", title: "Стоимость", num: true, html: (r) => fmt(r.value) },
      { key: "target", title: "У кого", html: (r) => esc(r.target || "") },
    ], { sort: "ts", empty: "—" });
    this.killTop = makeTable($("#kill-top"), [
      { key: "player", title: "Кто убивал вас чаще всего" },
      { key: "count", title: "Раз", num: true },
    ], { sort: "count", empty: "Вас не убивали — отлично." });
    this.kills = makeTable($("#kill-rows"), [
      { key: "ts", title: "Когда", html: (r) => dateTime(r.ts) },
      { key: "zone", title: "Где", html: (r) => esc(r.zone) },
      { key: "killer", title: "Убийца", html: (r) => `${esc(r.killer || "—")}${r.killer_guild ? ` <span class="muted">[${esc(r.killer_guild)}]</span>` : ""}` },
      { key: "victim", title: "Погибший", html: (r) => `${esc(r.victim || "—")}${r.victim_guild ? ` <span class="muted">[${esc(r.victim_guild)}]</span>` : ""}` },
      { key: "mine", title: "", sort: (r) => (r.my_death ? 2 : r.my_kill ? 1 : 0),
        html: (r) => (r.my_death ? '<span class="pill bad">вы погибли</span>' : r.my_kill ? '<span class="pill good">ваше убийство</span>' : "") },
    ], { sort: "ts", empty: "Смертей за период не было." });
    $("#loot-form").addEventListener("input", debounce(() => this.reload(), 250));
    this.reload = async () => {
      const f = $("#loot-form");
      const params = f.period.value === "session"
        ? { session: (await api("/api/session")).current, days: 3650 } : { days: f.period.value };
      const [data, kills] = await Promise.all([api("/api/loot", params), api("/api/kills", params)]);
      this.me = data.character;
      const who = f.player.value.trim().toLowerCase();
      const match = (name) => !who || (name || "").toLowerCase().includes(who);
      this.players.set(data.players.filter((p) => match(p.player)));
      this.rows.set(data.rows.filter((r) => match(r.actor)));
      const card = (v, l) => `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`;
      $("#kill-cards").innerHTML = card(fmt(kills.my_kills), "ваших убийств") + card(fmt(kills.my_deaths), "ваших смертей")
        + card(kills.my_deaths ? (kills.my_kills / kills.my_deaths).toFixed(2) : (kills.my_kills ? "∞" : "—"), "убийств на смерть")
        + card(fmt(kills.seen), "смертей рядом с вами");
      this.killTop.set(kills.top_killers);
      this.kills.set(kills.rows.filter((r) => match(r.killer) || match(r.victim)));
    };
    this.reload();
  },
  show() { if (this.reload) this.reload(); },
});

// ---------- персонаж ----------
App.tab({
  id: "character", group: "my", title: "Персонаж",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">То, что игра надёжно сообщает о персонаже: общая слава и её прирост по дням, баланс серебра,
        очки переспециализации, убийства и смерти. Разбивку славы по веткам специализаций протокол надёжно не отдаёт
        (у официального клиента этот обработчик отключён), поэтому данные характеристик, если придут, показаны как есть.</p>
      <form class="chart-controls" id="char-form"><label>Период <select name="days">
        <option value="7">неделя</option><option value="30" selected>месяц</option><option value="90">3 месяца</option>
        <option value="365">год</option></select></label></form>
      <div class="cards" id="char-cards"></div>
      <h2>Общая слава</h2><div id="char-fame"></div>
      <h2>Баланс серебра</h2><div id="char-balance"></div>
      <h2>Слава по дням</h2><div id="char-days"></div>
      <div id="char-stats"></div>`;
    this.days = makeTable($("#char-days"), [
      { key: "day", title: "День" },
      { key: "fame", title: "Получено славы", num: true, html: (r) => fmt(r.fame) },
    ], { sort: "day", empty: "Славы за период не получено." });
    $("#char-form").addEventListener("change", () => this.reload());
    this.reload = async () => {
      const d = await api("/api/character", { days: $("#char-form").days.value });
      const total = d.fame_total.length ? d.fame_total[d.fame_total.length - 1][1] : null;
      const card = (v, l) => `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`;
      $("#char-cards").innerHTML = [
        card(esc(d.character || "—"), "персонаж"),
        card(fmt(total), "всего славы"),
        card(fmt(d.fame_days.reduce((a, r) => a + r.fame, 0)), "славы за период"),
        card(d.balance.length ? fmt(d.balance[d.balance.length - 1][1]) : "—", "баланс серебра"),
        card(d.respec ? fmt(d.respec.points) : "—", "очков переспециализации"),
        card(`${d.kills} / ${d.deaths}`, "убийств / смертей за период"),
      ].join("");
      lineChart($("#char-fame"), [{ name: "Общая слава", slot: 1, points: d.fame_total }],
        { empty: "Нет данных: слава появится после первого получения славы в игре." });
      lineChart($("#char-balance"), [{ name: "Серебро на руках", slot: 2, points: d.balance }],
        { empty: "Нет данных: баланс приходит при изменении серебра в игре." });
      this.days.set(d.fame_days);
      $("#char-stats").innerHTML = d.stats
        ? `<h2>Данные характеристик (${dateTime(d.stats.ts)})</h2><pre>${esc(JSON.stringify(d.stats.data, null, 1))}</pre>` : "";
    };
    this.reload();
  },
  show() { if (this.reload) this.reload(); },
});

// ---------- сводка по зонам ----------
standardTab({
  id: "zones", group: "my", title: "Зоны",
  intro: "Где выгоднее фармить: всё, что вы получили в каждой зоне за период. Доход = серебро с мобов + поднятое серебро + "
    + "стоимость вашего лута по рынку. Время — сумма промежутков между событиями в зоне; паузы длиннее 10 минут "
    + "считаются отходом от игры и учитываются как 10 минут. Данные копятся сессиями из событий игры.",
  spec: [
    { legend: "Период", fields: [
      { type: "select", name: "days", label: "За", value: "30",
        options: [["1", "сутки"], ["7", "неделю"], ["30", "месяц"], ["90", "3 месяца"], ["3650", "всё время"]] },
      { type: "number", name: "min_minutes", label: "Мин. время в зоне, мин", value: 5, min: 0, step: 1 },
    ] },
  ],
  columns: [
    { key: "name", title: "Зона", html: (r) => esc(r.name), sort: (r) => r.name },
    { key: "visits", title: "Визитов", num: true, html: (r) => fmt(r.visits) },
    { key: "hours", title: "Время", num: true, html: (r) => duration(r.hours) },
    { key: "fame_per_hour", title: "Славы/ч", num: true, html: (r) => fmt(r.fame_per_hour) },
    { key: "income_per_hour", title: "Доход/ч", num: true, html: (r) => `<b>${fmt(r.income_per_hour)}</b>`, cls: () => "good" },
    { key: "fame", title: "Слава", num: true, html: (r) => fmt(r.fame) },
    { key: "silver", title: "Серебро с мобов", num: true, html: (r) => fmt(r.silver) },
    { key: "loot_value", title: "Лут по рынку", num: true,
      html: (r) => `${fmt(r.loot_value)}${r.unpriced_items ? ` <span class="muted" title="без цены: ${r.unpriced_items} предм.">*</span>` : ""}` },
    { key: "loot_silver", title: "Поднято серебра", num: true, html: (r) => fmt(r.loot_silver) },
    { key: "income", title: "Доход всего", num: true, html: (r) => fmt(r.income) },
    { key: "deaths", title: "Смертей", num: true, html: (r) => fmt(r.deaths), cls: (r) => (r.deaths ? "bad" : "") },
    { key: "last", title: "Последний раз", num: true, html: (r) => dateTime(r.last) },
  ],
  sort: "income_per_hour",
  empty: "Пока нет данных: сводка появится после игры с запущенным приложением.",
  async load(f) {
    const data = await api("/api/zones", { days: f.days });
    const minH = (Number(f.min_minutes) || 0) / 60;
    const rows = data.rows.filter((r) => r.hours >= minH || r.deaths);
    return { rows, summary: `Зон: ${rows.length}. * — часть лута без рыночной цены (не учтена в доходе).` };
  },
});
