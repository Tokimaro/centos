"use strict";
// Мир: цена золота и премиум, события мира.

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
