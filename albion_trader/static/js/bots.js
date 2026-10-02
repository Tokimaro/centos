"use strict";
// Вкладка «Боты»: окна игры на этом компьютере, сбор ресурсов и рынок для «живости»
// своего сервера, макросы рынка (запись кликов и правка).

const BOT_RES = [["wood", "дерево"], ["rock", "камень"], ["fiber", "волокно"], ["hide", "шкура"], ["ore", "руда"]];

App.tab({
  id: "bots", group: "bots", title: "Боты",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Боты управляют окнами игры на этом компьютере: собирают ресурсы рядом и выставляют заказы
        на рынке, чтобы ваш сервер выглядел живым. Нужны права администратора: и для чтения трафика, и для кликов
        в окне игры. Каждому окну — свой персонаж. <b>Только для своего сервера</b>: на официальных серверах за ботов банят.</p>
      <div class="bots-bar">
        <label><input type="checkbox" id="bots-enabled"> включить ботов</label>
        <label title="Нажатие останавливает всех ботов">остановка <select id="bots-stop-key"></select></label>
        <label title="Пока вы двигаете мышью или печатаете, боты ждут"><input type="checkbox" id="bots-pause"> ждать, пока я за компьютером</label>
        <label>пауза после моего ввода, с <input type="number" id="bots-idle" min="0" max="60" step="0.5"></label>
        <label title="После клика бота вернуть активное окно и курсор как было"><input type="checkbox" id="bots-restore"> возвращать окно и курсор</label>
        <button type="button" id="bots-stop-all" class="danger">Остановить всех</button>
      </div>
      <p class="bad" id="bots-msg" hidden></p>
      <div id="bots-list" class="bots-list"></div>
      <h3>Макросы рынка</h3>
      <div class="bots-macros">
        <div class="col">
          <label>Макрос <select id="macro-pick"></select></label>
          <label>Имя <input type="text" id="macro-name" maxlength="60" placeholder="например, открыть рынок"></label>
          <textarea id="macro-text" rows="10" spellcheck="false"></textarea>
          <div class="bots-row">
            <button type="button" id="macro-save">Сохранить</button>
            <button type="button" id="macro-delete">Удалить</button>
            <label>Записать клики в окне <select id="macro-window"></select></label>
            <button type="button" id="macro-rec">● Запись</button>
          </div>
          <span class="muted" id="macro-hint"></span>
        </div>
        <pre class="muted bots-help" id="macro-help"></pre>
      </div>`;
    this.cards = {};
    this.picked = "";
    const post = async (body, okText) => {
      try {
        const r = await apiPost("/api/bots", body);
        if (okText) this.message(okText, false);
        return r;
      } catch (e) { this.message(e.message, true); return null; }
    };
    this.post = post;
    const settings = () => post({
      action: "settings", enabled: $("#bots-enabled").checked, stop_key: $("#bots-stop-key").value,
      pause_when_active: $("#bots-pause").checked, user_idle: Number($("#bots-idle").value),
      restore_focus: $("#bots-restore").checked,
    }).then(() => this.poll());
    ["#bots-enabled", "#bots-stop-key", "#bots-pause", "#bots-idle", "#bots-restore"]
      .forEach((s) => $(s, el).addEventListener("change", settings));
    $("#bots-stop-all", el).addEventListener("click", () => post({ action: "stop_all" }, "все боты остановлены"));
    $("#macro-pick", el).addEventListener("change", (ev) => {
      this.picked = ev.target.value;
      $("#macro-name").value = this.picked;
      $("#macro-text").value = (this.data.macros || {})[this.picked] || "";
    });
    $("#macro-save", el).addEventListener("click", async () => {
      const name = $("#macro-name").value.trim();
      if (await post({ action: "save_macro", name, text: $("#macro-text").value }, `макрос «${name}» сохранён`)) {
        this.picked = name;
        this.poll();
      }
    });
    $("#macro-delete", el).addEventListener("click", async () => {
      const name = $("#macro-name").value.trim();
      if (name && confirm(`Удалить макрос «${name}»?`) && await post({ action: "delete_macro", name })) {
        this.picked = "";
        $("#macro-name").value = "";
        $("#macro-text").value = "";
        this.poll();
      }
    });
    $("#macro-rec", el).addEventListener("click", async () => {
      if (this.data.recording != null) {
        const r = await post({ action: "record_stop" });
        if (r) {
          const t = $("#macro-text");
          t.value = (t.value.trim() ? t.value.trimEnd() + "\n" : "") + r.text;
          $("#macro-hint").textContent = "Запись добавлена в текст — допишите type/key/expect и сохраните.";
        }
      } else if ($("#macro-window").value) {
        if (await post({ action: "record_start", pid: Number($("#macro-window").value) })) {
          $("#macro-hint").textContent = "Идёт запись: кликайте в окне игры, затем нажмите «Стоп».";
        }
      }
      this.poll();
    });
    this.poll();
  },
  show() {
    clearInterval(this.timer);
    this.timer = setInterval(() => { if (App.current === this && !document.hidden) this.poll(); }, 2000);
  },

  message(text, bad) {
    const m = $("#bots-msg");
    m.hidden = !text;
    m.textContent = text || "";
    m.className = bad ? "bad" : "good";
  },

  async poll() {
    let d;
    try { d = await api("/api/bots"); } catch (e) { this.message(e.message, true); return; }
    this.data = d;
    const keep = (sel, v) => { const x = $(sel); if (x && document.activeElement !== x) { if (x.type === "checkbox") x.checked = !!v; else x.value = v; } };
    const keySel = $("#bots-stop-key");
    if (!keySel.options.length) keySel.innerHTML = d.keys.filter((k) => k.length > 1 || /[a-z]/.test(k)).map((k) => `<option value="${k}">${k.toUpperCase()}</option>`).join("");
    keep("#bots-enabled", d.enabled);
    keep("#bots-stop-key", d.settings.stop_key);
    keep("#bots-pause", d.settings.pause_when_active);
    keep("#bots-idle", d.settings.user_idle);
    keep("#bots-restore", d.settings.restore_focus);
    $("#macro-help").textContent = d.macro_help;
    if (d.message) this.message(d.message, true);
    const macros = Object.keys(d.macros).sort();
    const pick = $("#macro-pick");
    if (pick.dataset.list !== macros.join("\n")) {
      pick.dataset.list = macros.join("\n");
      pick.innerHTML = `<option value="">— новый —</option>` + macros.map((m) => `<option>${esc(m)}</option>`).join("");
    }
    pick.value = this.picked;
    const winSel = $("#macro-window");
    const winOpts = d.windows.map((w) => `<option value="${w.pid}">${esc(w.character || `окно ${w.pid}`)}</option>`).join("");
    if (winSel.dataset.html !== winOpts) { winSel.dataset.html = winOpts; winSel.innerHTML = winOpts; }
    $("#macro-rec").textContent = d.recording != null ? "■ Стоп" : "● Запись";
    this.renderWindows(d, macros);
  },

  renderWindows(d, macros) {
    const list = $("#bots-list");
    if (!d.supported) {
      list.innerHTML = `<p class="muted">Боты работают только в Windows — там, где запущены окна игры.</p>`;
      return;
    }
    if (!d.enabled) {
      list.innerHTML = `<p class="muted">Включите ботов галочкой выше — программа начнёт искать окна игры и разбирать их трафик.</p>`;
      this.cards = {};
      return;
    }
    if (!d.windows.length) {
      list.innerHTML = `<p class="muted">Окна игры не найдены. Запустите клиенты Albion Online (несколько окон — несколько ботов).</p>`;
      this.cards = {};
      return;
    }
    const pids = d.windows.map((w) => String(w.pid));
    for (const pid of Object.keys(this.cards)) if (!pids.includes(pid)) { this.cards[pid].remove(); delete this.cards[pid]; }
    if (!list.querySelector(".bot-card")) list.innerHTML = "";
    for (const w of d.windows) {
      const key = `${w.pid}:${w.character}:${macros.join(",")}`;
      let card = this.cards[w.pid];
      if (!card || card.dataset.key !== key) {
        const fresh = this.card(w, macros);
        fresh.dataset.key = key;
        if (card) card.replaceWith(fresh); else list.appendChild(fresh);
        card = this.cards[w.pid] = fresh;
      }
      this.update(card, w);
    }
  },

  card(w, macros) {
    const c = w.config || { gather: {}, market: {} };
    const g = c.gather || {}, m = c.market || {};
    const el = document.createElement("div");
    el.className = "card bot-card";
    const opt = (v, t, cur) => `<option value="${v}"${String(cur) === String(v) ? " selected" : ""}>${esc(t)}</option>`;
    const macroSel = (k) => `<select data-m="${k}">${opt("", "—", m[k])}${macros.map((x) => opt(x, x, m[k])).join("")}</select>`;
    const num = (path, v, min, max, step = 1) => `<input type="number" data-k="${path}" value="${v ?? ""}" min="${min}" max="${max}" step="${step}">`;
    el.innerHTML = `
      <div class="bot-head"><b>${esc(w.character || "персонаж неизвестен")}</b>
        <span class="muted bot-where"></span><span class="bot-status"></span></div>
      ${w.character ? `
      <div class="bots-row">
        <label>Задача <select data-k="task">${Object.entries(this.data.tasks).map(([k, t]) => opt(k, t, c.task)).join("")}</select></label>
        <label title="С переключением окна — надёжно; сообщениями окну — без переключения, но не всегда работает">Ввод
          <select data-k="input">${opt("focus", "с переключением окна", c.input)}${opt("background", "без переключения (эксп.)", c.input)}</select></label>
        <label>работа, мин ${num("work_min", c.work_min, 1, 600)}</label>
        <label>отдых, мин ${num("rest_min", c.rest_min, 0, 600)}</label>
      </div>
      <details><summary>Сбор ресурсов</summary>
        <div class="bots-row">${BOT_RES.map(([r, n]) => `<label><input type="checkbox" data-res="${r}"${(g.res || []).includes(r) ? " checked" : ""}> ${n}</label>`).join("")}</div>
        <div class="bots-row">
          <label>тир от ${num("gather.tier_min", g.tier_min, 1, 8)}</label><label>до ${num("gather.tier_max", g.tier_max, 1, 8)}</label>
          <label>зачарование от ${num("gather.enchant_min", g.enchant_min, 0, 4)}</label>
          <label>радиус от старта, м ${num("gather.radius", g.radius, 10, 500, 5)}</label>
          <label title="0 — не обходить">обходить мобов, м ${num("gather.avoid_mobs", g.avoid_mobs, 0, 50)}</label>
          <label title="0 — без ограничения; потом бот просто гуляет">узлов за запуск ${num("gather.max_nodes", g.max_nodes, 0, 10000)}</label>
          <label><input type="checkbox" data-k="gather.avoid_players"${g.avoid_players ? " checked" : ""}> уходить от враждебных игроков</label>
        </div>
      </details>
      <details><summary>Рынок</summary>
        <div class="bots-row">
          <label class="col">Предметы (id через пробел или с новой строки)<textarea data-k="market.items" rows="2" placeholder="T4_BAG T5_2H_BOW">${esc(m.items || "")}</textarea></label>
        </div>
        <div class="bots-row">
          <label>Заказы <select data-k="market.side">${opt("sell", "на продажу", m.side)}${opt("buy", "на покупку", m.side)}</select></label>
          <label title="Продажа — на столько дешевле лучшей цены, покупка — дороже">шаг цены ${num("market.undercut", m.undercut, 0, 1e9)}</label>
          <label>кол-во ${num("market.qty", m.qty, 1, 9999)}</label>
          <label>пауза между заказами, мин ${num("market.interval_min", m.interval_min, 0.1, 600, 0.1)}–${num("market.interval_max", m.interval_max, 0.1, 600, 0.1)}</label>
        </div>
        <div class="bots-row">
          <label>открыть рынок ${macroSel("macro_open")}</label>
          <label>заказ ${macroSel("macro_order")}</label>
          <label>закрыть ${macroSel("macro_close")}</label>
        </div>
      </details>
      <div class="bots-row">
        <button type="button" data-a="start">▶ Старт</button>
        <button type="button" data-a="stop">■ Стоп</button>
        <button type="button" data-a="calibrate" title="Встаньте на открытое место: бот сделает 4 клика рядом с персонажем">Калибровка</button>
        <button type="button" data-a="test_click" title="Клик чуть ниже персонажа — проверка, что клики доходят до игры">Пробный клик</button>
        <span class="muted bot-calib"></span>
      </div>` : `<p class="muted">Бот узнаёт персонажа по входу в зону: смените зону (или перезайдите) в этом окне.</p>`}
      <div class="bot-log muted"></div>`;
    const pid = w.pid;
    const send = (patch) => this.post({ action: "configure", pid, ...patch });
    $$("[data-k]", el).forEach((inp) => inp.addEventListener("change", () => {
      const v = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? Number(inp.value) : inp.value;
      const [a, b] = inp.dataset.k.split(".");
      send(b ? { [a]: { [b]: v } } : { [a]: v });
    }));
    $$("[data-res]", el).forEach((inp) => inp.addEventListener("change", () => {
      send({ gather: { res: $$("[data-res]", el).filter((x) => x.checked).map((x) => x.dataset.res) } });
    }));
    $$("[data-m]", el).forEach((sel) => sel.addEventListener("change", () => send({ market: { [sel.dataset.m]: sel.value } })));
    $$("[data-a]", el).forEach((b) => b.addEventListener("click", async () => {
      const body = { action: b.dataset.a, pid };
      if (b.dataset.a === "start") body.task = $("[data-k=task]", el).value;
      if (b.dataset.a === "test_click") Object.assign(body, { x: 0.5, y: 0.62 });
      await this.post(body);
      this.poll();
    }));
    return el;
  },

  update(card, w) {
    const bot = w.bot;
    const where = [w.zone && `зона ${w.zone}`, w.pos && `(${w.pos[0]}, ${w.pos[1]})`,
      w.traffic ? `ресурсов рядом: ${w.resources}` : "нет трафика"].filter(Boolean).join(" · ");
    $(".bot-where", card).textContent = " " + where;
    const st = $(".bot-status", card);
    st.textContent = bot.status + (bot.gathered ? ` · собрано ${bot.gathered}` : "") + (bot.orders ? ` · заказов ${bot.orders}` : "");
    st.className = "bot-status " + (bot.status === "ошибка" ? "bad" : bot.running ? "good" : "muted");
    const calib = $(".bot-calib", card);
    if (calib) calib.textContent = w.config && w.config.calib && w.config.calib.measured ? "откалиброван" : "нужна калибровка";
    $(".bot-log", card).innerHTML = bot.log.slice(-6).reverse()
      .map((x) => `<div>${new Date(x.ts * 1000).toLocaleTimeString("ru-RU")} — ${esc(x.text)}</div>`).join("");
  },
});
