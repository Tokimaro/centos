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

// Небольшая карточка «значение + подпись».
function card(value, label, cls = "") {
  return `<div class="card"><div class="v ${cls}">${value}</div><div class="l">${esc(label)}</div></div>`;
}

// ---------- Сейчас: зона, персонаж, итоги сессии ----------
App.tab({
  id: "now", group: "tools", title: "Сейчас",
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
