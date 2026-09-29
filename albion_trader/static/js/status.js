"use strict";
// Статус сборщика и строка состояния в шапке.

// ---------- статус ----------
async function refreshConn() {
  try {
    const s = await api("/api/status");
    const c = s.capture;
    const el = $("#conn");
    const t = s.topics.find((x) => x.topic === "marketorders.ingest");
    const last = t ? `, данные ${age(t.last_at, s.now)} назад` : "";
    if (c.enabled && c.error) {
      el.textContent = `сборщик: ошибка — ${c.error}`; el.className = "conn bad";
    } else if (c.enabled && c.running) {
      const enc = c.encrypted_at && s.now - c.encrypted_at < 600;
      const loc = c.location_name ? ` · ${c.location_name}` : " · локация не определена — смените зону в игре, иначе цены не сохраняются";
      el.textContent = `сборщик работает${loc}${last}${enc ? " · данные рынка зашифрованы игрой" : ""}`;
      el.className = "conn " + (enc || !c.location_name ? "bad" : t && s.now - t.last_at < 600 ? "ok" : "old");
    } else {
      el.textContent = t ? `сборщик выключен${last}` : "сборщик выключен, данных нет";
      el.className = "conn old";
    }
  } catch { /* сервер недоступен */ }
}

async function loadStatus() {
  const s = await api("/api/status");
  $("#client-cmd").textContent = `albiondata-client.exe -i ${location.origin}${s.ingest_path}`;
  const locRows = s.locations.sort((a, b) => b.last_seen - a.last_seen).map((r) => `<tr>
      <td class="${r.location === "black_market" ? "bm" : ""}">${esc(r.name)}</td>
      <td>${r.auction_type === "offer" ? "предложения (продажа)" : "запросы (покупка)"}</td>
      <td class="num">${fmt(r.orders)}</td><td class="num">${fmt(r.items)}</td>
      <td class="num ${ageClass(r.last_seen, s.now)}">${age(r.last_seen, s.now)}</td></tr>`).join("");
  const topicRows = s.topics.map((t) => `<tr><td>${esc(t.topic)}</td><td class="num">${fmt(t.batches)}</td>
      <td class="num">${fmt(t.records)}</td><td class="num">${age(t.last_at, s.now)}</td></tr>`).join("");
  const c = s.capture;
  const capText = !c.enabled ? "выключен" : c.error ? "ошибка" : c.running ? "работает" : "остановлен";
  $("#status").innerHTML = `
    ${c.error ? `<p class="bad">Сборщик: ${esc(c.error)}</p>` : ""}
    ${c.market_responses_lost ? `<p class="warn">Ответов рынка не дошло целиком: ${fmt(c.market_responses_lost)} из ${fmt(c.market_requests)}. Похоже, теряются сетевые пакеты (в людных зонах это случается чаще) — обновите страницу рынка ещё раз.</p>` : ""}
    ${c.no_location_drops ? `<p class="warn">Страниц рынка без известной локации: ${fmt(c.no_location_drops)} — смените зону в игре.</p>` : ""}
    ${c.orders_by_content ? `<p class="warn">Заказы рынка приходят с неизвестным кодом операции (${fmt(c.orders_by_content)} раз) — похоже, игра обновилась.
      Цены сохраняются, но история цен, почта и часть событий могут не распознаваться.
      <button type="button" class="secondary" id="update-opcodes">Обновить коды операций</button></p>` : ""}
    ${c.encrypted_at ? `<p class="warn">Последний ответ рынка (${age(Math.round(c.encrypted_at), s.now)} назад) пришёл зашифрованным — игра сейчас не отдаёт цены в открытом виде.</p>` : ""}
    <div class="cards">
      <div class="card"><div class="v ${c.running ? "good" : "bad"}">${capText}</div><div class="l">встроенный сборщик · пакетов игры: ${fmt(c.packets)}</div></div>
      <div class="card"><div class="v">${esc(c.location_name || "—")}</div><div class="l">текущая локация${c.location ? "" : " — смените зону в игре"}</div></div>
      <div class="card"><div class="v">${fmt(c.order_batches)}</div><div class="l">страниц рынка собрано (${fmt(c.orders)} заказов) из ${fmt(c.market_requests)} запросов</div></div>
      <div class="card"><div class="v">${fmt(s.total_orders)}</div><div class="l">заказов в базе</div></div>
      <div class="card"><div class="v">${fmt(s.history_points)}</div><div class="l">точек истории продаж</div></div>
      <div class="card"><div class="v">${s.items_catalog ? fmt(s.items_catalog) : "нет"}</div><div class="l">названий предметов${s.items_catalog ? "" : " — выполните update-items"}</div></div>
      <div class="card"><div class="v">${s.gamedata_recipes ? fmt(s.gamedata_recipes) : "нет"}</div><div class="l">рецептов в справочнике${s.gamedata_recipes ? "" : " — выполните update-items"}</div></div>
    </div>
    <h2>Рынки</h2>
    <div class="table-wrap"><table><thead><tr><th>Рынок</th><th>Тип</th><th class="num">Заказов</th><th class="num">Предметов</th><th class="num">Обновлено</th></tr></thead>
      <tbody>${locRows || `<tr><td colspan="5" class="muted">Данных пока нет</td></tr>`}</tbody></table></div>
    <h2>Полученные пакеты</h2>
    <div class="table-wrap"><table><thead><tr><th>Топик</th><th class="num">Пакетов</th><th class="num">Записей</th><th class="num">Последний</th></tr></thead>
      <tbody>${topicRows || `<tr><td colspan="4" class="muted">Клиент ещё ничего не присылал</td></tr>`}</tbody></table></div>
    <p class="muted">После крупного обновления игры можно подтянуть актуальные номера операций из albiondata-client:
      <button type="button" class="secondary" id="update-opcodes-any">Обновить коды операций</button> <span id="opcodes-result"></span></p>`;
  for (const id of ["update-opcodes", "update-opcodes-any"]) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    btn.addEventListener("click", async () => {
      const out = $("#opcodes-result");
      out.textContent = "Обновляю…";
      try {
        const r = await apiPost("/api/update-opcodes", {});
        out.textContent = r.missing.length ? `Готово, но не найдены: ${r.missing.join(", ")}` : "Готово — коды обновлены и применены.";
      } catch (e) { out.textContent = `Ошибка: ${e.message}`; }
    });
  }
}


App.tab({ id: "status", group: "status", title: "Статус", show: loadStatus });
