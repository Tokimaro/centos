"use strict";
// Оповещения: конструктор правил, история, всплывающие окна, звук и
// системные уведомления браузера.

const ALERT_PREFS = "albion-trader-alert-prefs";
let alertKinds = {};
let lastAlertId = null;

function alertPrefs() { return { sound: true, ...loadSettings(ALERT_PREFS) }; }

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.15, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    o.start(); o.stop(ctx.currentTime + 0.4);
  } catch { /* звук недоступен */ }
}

function toast(alert) {
  let box = $("#toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast";
  const b = document.createElement("b"); b.textContent = alert.title;
  const t = document.createElement("div"); t.textContent = alert.text;
  el.append(b, t);
  el.addEventListener("click", () => {
    el.remove();
    if (alert.payload && alert.payload.item_id) App.openItem(alert.payload.item_id); else App.go("alerts");
  });
  box.appendChild(el);
  setTimeout(() => el.remove(), 15000);
}

function setBadge(n) {
  const btn = $('#groups [data-group="alerts"]');
  if (!btn) return;
  btn.dataset.badge = n > 0 ? (n > 99 ? "99+" : String(n)) : "";
}

async function pollAlerts() {
  try {
    const data = await api("/api/alerts", { since: lastAlertId || 0, limit: lastAlertId === null ? 1 : 50 });
    setBadge(data.unseen);
    if (lastAlertId === null) {           // первый запрос: старые оповещения не показываем
      lastAlertId = data.alerts.length ? data.alerts[0].id : 0;
      return;
    }
    const fresh = data.alerts.slice().reverse();
    if (!fresh.length) return;
    lastAlertId = Math.max(lastAlertId, ...fresh.map((a) => a.id));
    const prefs = alertPrefs();
    fresh.slice(-5).forEach(toast);
    if (prefs.sound) beep();
    if (window.Notification && Notification.permission === "granted") {
      for (const a of fresh.slice(-3)) {
        try { new Notification(a.title, { body: a.text, tag: `albion-${a.id}` }); } catch { /* нет поддержки */ }
      }
    }
    const t = App.byId("alerts");
    if (t && t.ready && App.current === t) t.reload();
  } catch { /* сервер недоступен */ }
}

// ---------- поля правил по типам ----------
function ruleSpec(kind) {
  const itemFilter = [
    { type: "search", name: "q", label: "Поиск предметов (пусто — все)", placeholder: "название или ID" },
    { type: "tiers" }, { type: "enchants" },
  ];
  if (kind === "price_below" || kind === "price_above") {
    return [
      { legend: "Предмет и цена", fields: [
        { type: "text", name: "item", label: "ID предмета", placeholder: "например, T4_BAG" },
        { type: "select", name: "side", label: "Какая цена", value: kind === "price_below" ? "sell" : "buy",
          options: [["sell", "предложения (за сколько продают)"], ["buy", "заказы на покупку (за сколько покупают)"]] },
        { type: "number", name: "price", label: kind === "price_below" ? "Сообщить, если цена ≤" : "Сообщить, если цена ≥", min: 0 },
        { type: "select", name: "quality", label: "Качество", value: "0",
          options: [["0", "любое"], ...[1, 2, 3, 4, 5].map((q) => [String(q), QUALITY[q]])] },
      ] },
      { legend: "Рынки (ничего не выбрано — все)", cls: "locs", fields: [{ type: "markets", name: "locations", default: [] }] },
    ];
  }
  if (kind === "deal") {
    return [
      { legend: "Покупать в", cls: "locs", fields: [{ type: "markets", name: "src", buyable: true }] },
      { legend: "Продавать в", cls: "locs", fields: [{ type: "markets", name: "dst" }] },
      { legend: "Условия", fields: [
        { type: "select", name: "buy", label: "Покупка", value: "instant", options: [["instant", "мгновенно"], ["order", "заказом"]] },
        { type: "select", name: "sell", label: "Продажа", value: "instant", options: [["instant", "мгновенно"], ["order", "предложением"]] },
        { type: "number", name: "min_profit", label: "Мин. прибыль / шт", value: 10000, min: 0 },
        { type: "number", name: "min_margin", label: "Мин. маржа, %", value: 15, min: 0 },
        { type: "number", name: "min_total", label: "Мин. общая прибыль", value: 100000, min: 0 },
        { type: "number", name: "max_age", label: "Свежесть цен, ч", value: 2, min: 0.1 },
      ] },
      { legend: "Предметы", fields: itemFilter },
    ];
  }
  if (kind === "underpriced") {
    return [
      { legend: "Рынки (ничего не выбрано — все)", cls: "locs", fields: [{ type: "markets", name: "locations", buyable: true, default: [] }] },
      { legend: "Условия", fields: [
        { type: "number", name: "min_discount", label: "Мин. скидка, %", value: 30, min: 0 },
        { type: "number", name: "min_profit", label: "Мин. прибыль / шт", value: 5000, min: 0 },
        { type: "number", name: "max_age", label: "Свежесть, ч", value: 1, min: 0.1 },
      ] },
      { legend: "Предметы", fields: itemFilter },
    ];
  }
  return [{ legend: "Параметры", fields: [] }];
}

function ruleSummary(r) {
  const p = r.params || {};
  const locs = (l) => (l && l.length ? l.map((x) => App.locName(x)).join(", ") : "все рынки");
  switch (r.kind) {
    case "price_below": case "price_above":
      return `${esc(p.item || "?")}: ${p.side === "buy" ? "заказ на покупку" : "предложение"} ${r.kind === "price_below" ? "≤" : "≥"} ${fmt(Number(p.price))}; ${esc(locs(p.locations))}`;
    case "deal":
      return `${esc(locs(p.src))} → ${esc(locs(p.dst))}; прибыль от ${fmt(Number(p.min_profit))}/шт, ${p.min_margin || 0}%`;
    case "underpriced":
      return `скидка от ${p.min_discount}%; прибыль от ${fmt(Number(p.min_profit))}; ${esc(locs(p.locations))}`;
    case "outbid": return "ваш заказ на рынке больше не лучший";
    case "world_event": return "нападение бандитов, фестивали";
    case "radar_hostile": return "враждебный игрок подошёл ближе заданного на вкладке «Радар»";
    case "bot": return "бот остановился, персонаж погиб, рядом игроки, данж пройден";
    default: return "";
  }
}

App.tab({
  id: "alerts", group: "alerts", title: "Оповещения",
  async init(el) {
    el.innerHTML = `
      <p class="muted intro">Правила проверяются при каждом просмотре рынка в игре. Сработавшее правило показывает
        всплывающее окно, звук и системное уведомление (если разрешено); одно и то же срабатывание повторяется не чаще
        раза в 30 минут. Оповещения приходят, пока открыта эта страница.</p>
      <div class="buttons">
        <button type="button" class="secondary" id="notif-perm">Разрешить системные уведомления</button>
        <label class="inline"><input type="checkbox" id="alert-sound"> Звук</label>
        <button type="button" class="secondary" id="alerts-seen">Отметить все прочитанными</button>
      </div>
      <details id="channels"><summary><b>Отправлять оповещения в Telegram или Discord</b></summary>
        <p class="muted">Сообщения уходят только в ваш собственный бот/канал и только когда вы это включили.
          Telegram: создайте бота у @BotFather (получите токен), напишите ему любое сообщение и узнайте свой chat id
          (например, у @userinfobot). Discord: настройки канала → Интеграции → Вебхуки → «Копировать URL».</p>
        <form class="filters" id="channels-form" autocomplete="off">
          <fieldset class="opts"><legend>Telegram</legend>
            <label class="inline"><input type="checkbox" name="telegram_enabled"> Включить</label>
            <label>Токен бота <input type="password" name="telegram_token" placeholder="123456:ABC…"></label>
            <label>Chat id <input type="text" name="telegram_chat_id" placeholder="123456789"></label>
          </fieldset>
          <fieldset class="opts"><legend>Discord</legend>
            <label class="inline"><input type="checkbox" name="discord_enabled"> Включить</label>
            <label>URL вебхука <input type="password" name="discord_webhook" placeholder="https://discord.com/api/webhooks/…"></label>
          </fieldset>
          <fieldset class="opts"><legend>Что отправлять (ничего не выбрано — всё)</legend>
            <div class="checks" id="channel-kinds"></div>
            <div class="buttons">
              <button type="submit" class="primary">Сохранить</button>
              <button type="button" class="secondary" id="channels-test">Проверить</button>
            </div>
            <div class="summary" id="channels-result"></div>
          </fieldset>
        </form>
      </details>
      <h2>Новое правило</h2>
      <label class="rule-kind">Тип <select id="rule-kind"></select></label>
      <label class="rule-kind">Название <input type="text" id="rule-name" placeholder="необязательно"></label>
      <div id="rule-form"></div>
      <h2>Правила</h2><div id="rules-table"></div>
      <h2>История</h2><div id="alerts-table"></div>`;
    const prefs = alertPrefs();
    $("#alert-sound").checked = prefs.sound;
    $("#alert-sound").addEventListener("change", (e) => saveSettings({ ...alertPrefs(), sound: e.target.checked }, ALERT_PREFS));
    $("#notif-perm").addEventListener("click", async () => {
      if (!window.Notification) { alert("Браузер не поддерживает уведомления"); return; }
      const res = await Notification.requestPermission();
      $("#notif-perm").textContent = res === "granted" ? "Уведомления разрешены" : "Уведомления запрещены в браузере";
    });
    if (window.Notification && Notification.permission === "granted") $("#notif-perm").textContent = "Уведомления разрешены";
    $("#alerts-seen").addEventListener("click", async () => { await apiPost("/api/alerts/seen", {}); this.reload(); });

    const data = await api("/api/alert-rules");
    alertKinds = data.kinds;
    const settings = await api("/api/settings");
    const cf = $("#channels-form");
    for (const k of ["telegram_token", "telegram_chat_id", "discord_webhook"]) cf.querySelector(`[name=${k}]`).value = settings[k] || "";
    for (const k of ["telegram_enabled", "discord_enabled"]) cf.querySelector(`[name=${k}]`).checked = !!settings[k];
    checkboxList($("#channel-kinds"), "notify_kinds",
      Object.entries(alertKinds).map(([value, label]) => ({ value, label })), settings.notify_kinds || []);
    const readChannels = () => ({
      telegram_enabled: cf.querySelector("[name=telegram_enabled]").checked,
      telegram_token: cf.querySelector("[name=telegram_token]").value.trim(),
      telegram_chat_id: cf.querySelector("[name=telegram_chat_id]").value.trim(),
      discord_enabled: cf.querySelector("[name=discord_enabled]").checked,
      discord_webhook: cf.querySelector("[name=discord_webhook]").value.trim(),
      notify_kinds: $$("input[name=notify_kinds]:checked", cf).map((i) => i.value),
    });
    cf.addEventListener("submit", async (e) => {
      e.preventDefault();
      await apiPost("/api/settings", readChannels());
      $("#channels-result").textContent = "Сохранено.";
    });
    $("#channels-test").addEventListener("click", async () => {
      const out = $("#channels-result");
      try {
        await apiPost("/api/settings", readChannels());
        const r = await apiPost("/api/notify-test", {});
        out.innerHTML = r.results.map((x) => `${x.channel}: ${x.ok ? '<span class="good">отправлено</span>'
          : `<span class="bad">ошибка — ${esc(x.error || `HTTP ${x.status}`)}</span>`}`).join("<br>");
      } catch (e) { out.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
    });
    $("#rule-kind").innerHTML = Object.entries(alertKinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    const buildRuleForm = () => {
      const kind = $("#rule-kind").value;
      const box = $("#rule-form");
      box.innerHTML = "";
      this.ruleForm = buildForm(box, `albion-trader-rule-${kind}`, ruleSpec(kind), {
        submitText: "Добавить правило",
        onSubmit: async (values) => {
          const params = { ...values };
          for (const k of ["price", "quality", "min_profit", "min_margin", "min_total", "max_age", "min_discount"]) {
            if (params[k] !== undefined && params[k] !== "") params[k] = Number(params[k]);
          }
          try {
            await apiPost("/api/alert-rules", { action: "save", rule: { kind, name: $("#rule-name").value, params } });
            $("#rule-name").value = "";
            this.reload();
          } catch (e) { alert(`Не удалось сохранить: ${e.message}`); }
        },
      });
    };
    $("#rule-kind").addEventListener("change", buildRuleForm);
    buildRuleForm();

    this.rulesTable = makeTable($("#rules-table"), [
      { key: "name", title: "Название" },
      { key: "kind", title: "Тип", html: (r) => esc(alertKinds[r.kind] || r.kind) },
      { key: "params", title: "Условие", html: ruleSummary, sort: (r) => r.kind },
      { key: "enabled", title: "Вкл.", html: (r) => `<input type="checkbox" data-toggle="${r.id}"${r.enabled ? " checked" : ""}>` },
      { key: "id", title: "", html: (r) => `<span class="row-actions"><button type="button" data-delete="${r.id}">Удалить</button></span>` },
    ], { empty: "Правил пока нет." });
    this.alertsTable = makeTable($("#alerts-table"), [
      { key: "ts", title: "Когда", html: (r) => dateTime(r.ts) },
      { key: "title", title: "Оповещение", html: (r) => `${r.seen ? "" : '<span class="pill warn">новое</span> '}<b>${esc(r.title)}</b>` },
      { key: "text", title: "Подробности" },
    ], { sort: "ts", empty: "Оповещений пока не было." });
    el.addEventListener("change", async (e) => {
      const id = e.target.dataset && e.target.dataset.toggle;
      if (id) { await apiPost("/api/alert-rules", { action: "toggle", id: Number(id), enabled: e.target.checked }); this.reload(); }
    });
    el.addEventListener("click", async (e) => {
      const id = e.target.dataset && e.target.dataset.delete;
      if (id && confirm("Удалить правило?")) { await apiPost("/api/alert-rules", { action: "delete", id: Number(id) }); this.reload(); }
    });
    this.reload = async () => {
      const [rules, list] = await Promise.all([api("/api/alert-rules"), api("/api/alerts", { limit: 200 })]);
      this.rulesTable.set(rules.rules);
      this.alertsTable.set(list.alerts);
      setBadge(list.unseen);
    };
    this.reload();
  },
  show() { if (this.reload) this.reload(); },
});

// Быстрое правило «цена ниже» со страницы предмета.
function presetPriceAlert(itemId) {
  saveSettings({ ...loadSettings("albion-trader-rule-price_below"), item: itemId }, "albion-trader-rule-price_below");
  App.go("alerts");
  setTimeout(() => {
    const kind = $("#rule-kind");
    if (kind) { kind.value = "price_below"; kind.dispatchEvent(new Event("change")); }
  }, 300);
}

pollAlerts();
setInterval(pollAlerts, 10000);
