"use strict";
// Мир: события мира, цена золота и премиум.

function countdown(ts, now) {
  const s = Math.round(ts - now);
  if (s <= 0) return "завершено";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d} д ${h} ч`;
  return h ? `${h} ч ${m} мин` : `${m} мин ${String(sec).padStart(2, "0")} с`;
}

App.tab({
  id: "events", group: "world", title: "События",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Нападения бандитов и фестивали. Данные приходят, когда вы открываете карту мира в игре
        (иногда и сами). Правило оповещений «События мира» включено по умолчанию — его можно выключить во вкладке
        «Оповещения».</p>
      <div class="cards" id="world-cards"></div>
      <h2>Фестивали</h2><div id="world-fests"></div>`;
    this.fests = makeTable($("#world-fests"), [
      { key: "name", title: "Событие" },
      { key: "category", title: "Категория", html: (r) => esc(r.data.category || "—"), sort: (r) => r.data.category || "" },
      { key: "start_ts", title: "Начало", html: (r) => dateTime(r.start_ts) },
      { key: "end_ts", title: "Конец", html: (r) => dateTime(r.end_ts) },
      { key: "status", title: "Статус", sort: (r) => r.start_ts,
        html: (r) => (r.end_ts <= this.now ? '<span class="muted">завершено</span>'
          : r.start_ts > this.now ? `<span class="pill warn">через ${countdown(r.start_ts, this.now)}</span>`
            : `<span class="pill good">идёт, ещё ${countdown(r.end_ts, this.now)}</span>`) },
    ], { sort: "start_ts", asc: true, empty: "Фестивалей не видно. Откройте карту мира в игре." });
    const render = () => {
      const b = this.data && this.data.bandit;
      const now = Date.now() / 1000;
      $("#world-cards").innerHTML = b
        ? `<div class="card"><div class="v">${b.end_ts > now ? countdown(b.end_ts, now) : "завершено"}</div>
             <div class="l">нападение бандитов: фаза ${b.phase ?? "?"} (${esc(b.phase_name)}) до конца фазы${b.data.provinces && b.data.provinces.length ? ` · провинции: ${esc(b.data.provinces.join(", "))}` : ""}</div></div>
           <div class="card"><div class="v">${dateTime(b.end_ts)}</div><div class="l">окончание фазы · обновлено ${age(b.updated, now)} назад</div></div>`
        : `<div class="card"><div class="v">—</div><div class="l">нападение бандитов: данных нет (откройте карту мира)</div></div>`;
    };
    this.reload = async () => {
      this.data = await api("/api/world");
      this.now = this.data.now;
      this.fests.set(this.data.festivals);
      render();
    };
    setInterval(() => { if (App.current === App.byId("events")) render(); }, 1000);
    this.reload();
  },
  show() { if (this.reload) this.reload(); },
});

App.tab({
  id: "gold", group: "world", title: "Золото и премиум",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Курс золота собирается, когда вы открываете в игре биржу золота (график цены).
        Калькулятор премиума сравнивает его стоимость в серебре по текущему курсу с экономией на налоге:
        с премиумом налог с продаж 4% вместо 8%.</p>
      <form class="chart-controls" id="gold-form"><label>Период <select name="days">
        <option value="1">сутки</option><option value="7">неделя</option><option value="30" selected>месяц</option>
        <option value="90">3 месяца</option></select></label></form>
      <div class="cards" id="gold-cards"></div>
      <div id="gold-chart"></div>
      <h2>Окупится ли премиум</h2>
      <form class="filters row" id="prem-form" autocomplete="off">
        <label>Премиум стоит, золота <input type="number" name="gold" value="3750" min="0" step="any"
          title="Проверьте цену в игровом магазине"></label>
        <label>Курс, серебра за золото <input type="number" name="rate" min="0" step="any"></label>
        <label>Ваши продажи на рынке за 30 дней, серебро <input type="number" name="turnover" min="0" step="any"></label>
      </form>
      <div class="summary" id="prem-result"></div>`;
    const calc = () => {
      const f = $("#prem-form");
      const gold = Number(f.gold.value) || 0, rate = Number(f.rate.value) || 0, turnover = Number(f.turnover.value) || 0;
      if (!gold || !rate) { $("#prem-result").textContent = "Укажите стоимость премиума и курс."; return; }
      const cost = gold * rate, saving = turnover * 0.04, breakEven = cost / 0.04;
      $("#prem-result").innerHTML = `Премиум стоит <b>${fmt(cost)}</b> серебра. Экономия налога при ваших продажах —
        <b class="${saving >= cost ? "good" : "bad"}">${fmt(saving)}</b>. Окупается при продажах от <b>${fmt(breakEven)}</b>
        серебра в месяц (без учёта других бонусов премиума: слава, добыча, обучение).`;
    };
    $("#prem-form").addEventListener("input", calc);
    $("#gold-form").addEventListener("change", () => this.reload());
    this.reload = async () => {
      const data = await api("/api/gold", { days: $("#gold-form").days.value });
      const prices = data.prices.map((r) => r.price);
      $("#gold-cards").innerHTML = `
        <div class="card"><div class="v">${data.current ? fmt(data.current.price) : "—"}</div><div class="l">текущий курс${data.current ? ` · ${age(data.current.ts, data.now)} назад` : ""}</div></div>
        <div class="card"><div class="v">${prices.length ? fmt(Math.min(...prices)) : "—"}</div><div class="l">минимум за период</div></div>
        <div class="card"><div class="v">${prices.length ? fmt(Math.max(...prices)) : "—"}</div><div class="l">максимум за период</div></div>`;
      lineChart($("#gold-chart"), [{ name: "Серебра за 1 золото", slot: 1, points: data.prices.map((r) => [r.ts, r.price]) }],
        { empty: "Курса пока нет: откройте в игре биржу золота." });
      const f = $("#prem-form");
      if (!f.rate.value && data.current) f.rate.value = data.current.price;
      if (!f.turnover.value && data.sold_30d) f.turnover.value = Math.round(data.sold_30d);
      calc();
    };
    this.reload();
  },
});
