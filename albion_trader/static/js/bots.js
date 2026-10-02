"use strict";
// Вкладка «Боты»: один бот для окна игры — сбор ресурсов, рынок, перевозка между
// городами, данж. Здесь же калибровка, места, точки интерфейса игры и свои макросы.

const BOT_RES = [["wood", "дерево"], ["rock", "камень"], ["fiber", "волокно"], ["hide", "шкура"], ["ore", "руда"]];

const BOT_HELP = {
  gather: `Бот ищет на радаре своего окна ближайший подходящий ресурс в радиусе от точки старта, подходит
    к нему короткими шагами (не задевая чужие узлы), кликает по узлу и ждёт, пока тот не истощится. Ресурсов нет —
    гуляет по округе. Рядом враждебный игрок — отходит к точке старта. Три узла подряд не собираются (полная сумка,
    нет инструмента) — останавливается.`,
  market: `<ol>
    <li><b>Где стоять.</b> Встаньте вплотную к торговцу рынка и нажмите «Запомнить место» (например, «рынок Тетфорд»).
      Выберите его ниже — бот сам дойдёт туда, даже из другой зоны. Если место не выбрано, бот работает там, где стоит.</li>
    <li><b>Точки интерфейса.</b> В разделе «Точки интерфейса» один раз покажите кликом в окне игры: торговца, вкладку
      «Продать» или «Купить», поле поиска, первый предмет в результатах, кнопку «Заказ на продажу/покупку», поля цены и
      количества, кнопку подтверждения. Делайте это в том же размере окна, в котором будет работать бот.</li>
    <li><b>Что делает бот.</b> Для каждого заказа: берёт случайный предмет из списка, узнаёт лучшую цену <i>этого</i>
      рынка из данных, которые программа собрала (откройте рынок в игре хоть раз, чтобы цены появились), ставит цену на
      «шаг» дешевле лучшей продажи или дороже лучшей покупки, затем кликает по точкам: торговец → вкладка → поиск →
      вводит название → первый предмет → «Заказ» → цена → количество → подтверждение → Esc. После подтверждения бот
      проверяет, что игра отправила запрос серверу, иначе останавливается с ошибкой.</li>
    <li><b>Ритм.</b> Между заказами — случайная пауза в заданных пределах; после «работы» — «отдых».</li></ol>
    Если в вашем клиенте шаги другие, запишите свой макрос (ниже, «Свои макросы») и выберите его вместо готового.`,
  transport: `Бот возит ресурсы между городами: идёт к <b>месту погрузки</b> (город А), выполняет макрос погрузки
    (например, открыть банк и забрать вещи — запишите его кликами), садится на маунта (клавиша), идёт к <b>месту
    разгрузки</b> (город Б) по маршруту через зоны допустимой опасности и там выполняет макрос разгрузки или
    выставляет предметы из списка на продажу по ценам рынка Б. «Туда и обратно» — повторять рейсы. Маршрут строится по
    выходам зон из справочника игры, путь внутри зоны — в обход построек и воды по схеме зоны.`,
  dungeon_run: `<ol>
    <li><b>Подготовка.</b> Встаньте у сундука в центре города и нажмите «Запомнить место» (например, «сундук Тетфорд»),
      выберите его ниже. В «Точках интерфейса» укажите сундук, кнопку «Положить всё» и «Взять всё» (добыча), а если
      игра спрашивает подтверждение входа в данж — кнопку «Войти».</li>
    <li><b>Поиск.</b> Бот выходит из города в ближайшую зону допустимой опасности, бегает по ней (по схеме зоны — только
      по проходимому) и ищет на радаре вход в данж нужного вида: по умолчанию зелёные (соло). У портала игроки — не
      заходит. Не нашёл за отведённое время — идёт в следующую ближайшую зону.</li>
    <li><b>Данж.</b> Этаж за этажом: бой с мобами (клик по мобу и умения по перезарядке), добыча и сундуки, разведка;
      этаж пуст — выход на следующий этаж. Убит босс, открыт финальный сундук и собрано всё вокруг — данж пройден.</li>
    <li><b>Игроки.</b> Радар следит за игроками: в открытом мире бот отходит от них, в данже — бросает его и выходит тем
      же путём, потом ищет другой данж.</li>
    <li><b>Домой.</b> Выход тем же путём (этаж за этажом к точкам появления), дорога в город к сундуку, «Положить всё» —
      и снова поиск. «Данжей за запуск» 0 — без ограничения.</li>
    <li><b>Быстрый выход.</b> Если задана клавиша (в игре — A), бот выходит из данжа ею: перед нажатием добивает мобов
      рядом, следит, чтобы урон не сбил задержку, и повторяет при необходимости. Не вышло — выходит пешком.</li></ol>
    Здоровье ниже порога — зелье, ещё ниже — отход к точке появления на этаже. Персонаж погиб — бот останавливается.`,
  dungeon: `Пройти данж, в котором стоит персонаж: этажи, мобы, добыча, босс и финальный сундук. Игроки рядом — выход из
    данжа тем же путём. Настройки — те же, что у «Данжи по кругу».`,
  schedule: `Бот работает по расписанию: в каждом окне времени — своя задача (с её настройками) и, если указан,
    свой персонаж (бот переключится на его окно). Вне окон бот ждёт. Окно закончилось — задача прерывается, начинается
    следующая. Ошибка в задаче не останавливает расписание: бот ждёт следующего окна и присылает оповещение.`,
  wander: `Бот просто гуляет по округе от точки старта (радиус — как у сбора), чтобы мир выглядел живым.`,
};

App.tab({
  id: "bots", group: "bots", title: "Боты",
  init(el) {
    el.innerHTML = `
      <p class="muted intro">Бот управляет окном игры на этом компьютере (активным при запуске) и видит мир через её
        трафик. Работает один бот за раз. Нужны права администратора. <b>Только для своего сервера</b>: на официальных
        серверах за ботов банят.</p>
      <div class="bots-bar">
        <label><input type="checkbox" data-set="enabled"> включить бота</label>
        <label title="Нажатие останавливает бота">остановка <select data-set="stop_key"></select></label>
        <label title="Пока вы двигаете мышью или печатаете, бот ждёт"><input type="checkbox" data-set="pause_when_active"> ждать, пока я за компьютером</label>
        <label>пауза после моего ввода, с <input type="number" data-set="user_idle" min="0" max="60" step="0.5"></label>
        <label title="После клика бота вернуть активное окно и курсор"><input type="checkbox" data-set="restore_focus"> возвращать окно и курсор</label>
        <label title="С переключением окна — надёжно; без переключения — не отнимает мышь, но не все клиенты принимают">ввод
          <select data-set="input"><option value="focus">с переключением окна</option><option value="background">без переключения (эксп.)</option></select></label>
        <label title="Нет прогресса столько минут — бот пробует выбраться, потом останавливается и присылает оповещение. 0 — выключено">сторож, мин
          <input type="number" data-set="watchdog_min" min="0" max="120" step="1"></label>
        <label title="Трафик окна игры и действия бота пишутся в data/bot_sessions — для разбора ошибок (python -m albion_trader bot-session)">
          <input type="checkbox" data-set="record"> записывать сессии</label>
      </div>
      <p class="muted">Оповещения о гибели, остановке, игроках рядом и пройденных данжах — правило «Бот» на вкладке
        «Оповещения» (Windows, Telegram, Discord). Путь и цель бота видны на радаре.</p>
      <p id="bots-msg" hidden></p>
      <div class="card bot-game" id="bot-game"></div>
      <div class="card">
        <div class="bot-tasks" id="bot-tasks"></div>
        <div class="bots-row">
          <button type="button" id="bot-start">▶ Старт</button>
          <button type="button" id="bot-stop" class="danger">■ Стоп</button>
          <span id="bot-status" class="bot-status"></span>
        </div>
        <ul class="bot-check" id="bot-check"></ul>
        <details class="bot-help"><summary>Как это работает</summary><div id="bot-help"></div></details>
        <div id="bot-form"></div>
        <div class="bot-log muted" id="bot-log"></div>
      </div>
      <div class="card">
        <div class="bots-row"><b>Профили настроек</b>
          <select id="profile-pick"></select>
          <button type="button" id="profile-load">Загрузить</button>
          <button type="button" id="profile-delete">Удалить</button>
          <input type="text" id="profile-name" maxlength="60" placeholder="имя нового профиля">
          <button type="button" id="profile-save">Сохранить текущие</button></div>
        <div id="bot-stats"></div>
      </div>
      <h3>Точки интерфейса игры</h3>
      <p class="muted">Нужны рынку и сбору добычи. Нажмите «Указать», переключитесь в окно игры и кликните по нужной
        кнопке или полю (окно должно быть открыто). Координаты запоминаются в долях окна — указывайте их в том размере окна,
        в котором работает бот. <span id="bot-points-size"></span></p>
      <div id="bot-points"></div>
      <h3>Места</h3>
      <p class="muted">Встаньте в игре в нужном месте (у банка, у торговца рынка) и сохраните его — бот будет ходить туда сам.</p>
      <div class="bots-row"><input type="text" id="place-name" maxlength="60" placeholder="например, банк Тетфорд">
        <button type="button" id="place-save">Запомнить место</button></div>
      <div id="bot-places"></div>
      <details class="bots-macros-box"><summary><h3>Свои макросы</h3></summary>
        <div class="bots-macros">
          <div class="col">
            <label>Макрос <select id="macro-pick"></select></label>
            <label>Имя <input type="text" id="macro-name" maxlength="60" placeholder="например, погрузка в банке"></label>
            <textarea id="macro-text" rows="10" spellcheck="false"></textarea>
            <div class="bots-row">
              <button type="button" id="macro-save">Сохранить</button>
              <button type="button" id="macro-delete">Удалить</button>
              <button type="button" id="macro-rec">● Запись кликов</button>
              <label>шаблон <select id="macro-template"><option value="">—</option></select></label>
            </div>
            <span class="muted" id="macro-hint"></span>
          </div>
          <pre class="muted bots-help" id="macro-help"></pre>
        </div>
      </details>`;
    this.picked = "";
    this.task = null;
    this.formKey = "";
    const post = async (body, okText) => {
      try {
        const r = await apiPost("/api/bots", body);
        if (okText) this.message(okText, false);
        return r;
      } catch (e) { this.message(e.message, true); return null; }
    };
    this.post = post;
    $$("[data-set]", el).forEach((inp) => inp.addEventListener("change", async () => {
      const v = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? Number(inp.value) : inp.value;
      await post({ action: "settings", [inp.dataset.set]: v });
      this.poll();
    }));
    $("#bot-start", el).addEventListener("click", async () => { await post({ action: "start", task: this.task }); this.poll(); });
    $("#bot-stop", el).addEventListener("click", async () => { await post({ action: "stop" }); this.poll(); });
    $("#profile-save", el).addEventListener("click", async () => {
      if (await post({ action: "save_profile", name: $("#profile-name").value.trim() })) { $("#profile-name").value = ""; this.poll(); }
    });
    $("#profile-load", el).addEventListener("click", async () => {
      if ($("#profile-pick").value && await post({ action: "load_profile", name: $("#profile-pick").value })) { this.formKey = ""; this.task = null; this.poll(); }
    });
    $("#profile-delete", el).addEventListener("click", async () => {
      const name = $("#profile-pick").value;
      if (name && confirm(`Удалить профиль «${name}»?`) && await post({ action: "delete_profile", name })) this.poll();
    });
    $("#place-save", el).addEventListener("click", async () => {
      if (await post({ action: "save_place", name: $("#place-name").value.trim() })) { $("#place-name").value = ""; this.poll(); }
    });
    $("#macro-pick", el).addEventListener("change", (ev) => {
      this.picked = ev.target.value;
      $("#macro-name").value = this.picked;
      $("#macro-text").value = (this.data.macros || {})[this.picked] || "";
    });
    $("#macro-template", el).addEventListener("change", (ev) => {
      if (ev.target.value) $("#macro-text").value = this.data.templates[ev.target.value];
      ev.target.value = "";
    });
    $("#macro-save", el).addEventListener("click", async () => {
      const name = $("#macro-name").value.trim();
      if (await post({ action: "save_macro", name, text: $("#macro-text").value }, `макрос «${name}» сохранён`)) {
        this.picked = name;
        this.formKey = "";
        this.poll();
      }
    });
    $("#macro-delete", el).addEventListener("click", async () => {
      const name = $("#macro-name").value.trim();
      if (name && confirm(`Удалить макрос «${name}»?`) && await post({ action: "delete_macro", name })) {
        this.picked = "";
        $("#macro-name").value = $("#macro-text").value = "";
        this.formKey = "";
        this.poll();
      }
    });
    $("#macro-rec", el).addEventListener("click", async () => {
      const rec = this.data.recording;
      if (rec && !rec.point) {
        const r = await post({ action: "record_stop" });
        if (r) {
          const t = $("#macro-text");
          t.value = (t.value.trim() ? t.value.trimEnd() + "\n" : "") + r.text;
          $("#macro-hint").textContent = "Записано — допишите type/key/expect и сохраните.";
        }
      } else if (await post({ action: "record_start" })) {
        $("#macro-hint").textContent = "Идёт запись: кликайте в окне игры, затем нажмите «Стоп».";
      }
      this.poll();
    });
    this.poll();
  },
  show() {
    clearInterval(this.timer);
    this.timer = setInterval(() => { if (App.current === this && !document.hidden) this.poll(); }, 1500);
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
    const keySel = $("[data-set=stop_key]");
    if (!keySel.options.length) {
      keySel.innerHTML = d.keys.filter((k) => k.length > 1).map((k) => `<option value="${k}">${k.toUpperCase()}</option>`).join("");
    }
    $$("[data-set]").forEach((x) => {
      if (document.activeElement === x) return;
      const v = d.settings[x.dataset.set];
      if (x.type === "checkbox") x.checked = !!v; else x.value = v;
    });
    if (d.message) this.message(d.message, false);
    if (this.task === null) this.task = d.settings.task || "gather";
    this.renderGame(d);
    this.renderTasks(d);
    this.renderPoints(d);
    this.renderPlaces(d);
    this.renderMacros(d);
    this.renderExtras(d);
  },

  renderGame(d) {
    const box = $("#bot-game");
    if (!d.supported) { box.innerHTML = `<span class="muted">Бот работает только в Windows — там, где запущена игра.</span>`; return; }
    if (!d.enabled) { box.innerHTML = `<span class="muted">Включите бота галочкой выше — программа начнёт следить за окном игры.</span>`; return; }
    const g = d.game;
    if (!g) { box.innerHTML = `<span class="muted">Окно игры не найдено. Запустите Albion Online.</span>`; return; }
    const c = g.counts || {};
    const html = `<div class="bot-head"><b>${esc(g.character || "персонаж неизвестен — смените зону в игре")}</b>
        <span class="muted">${esc([g.zone_name || g.zone, g.pos && `(${g.pos[0]}, ${g.pos[1]})`, g.hp != null && `HP ${g.hp}%`,
          g.traffic ? `ресурсов ${c.resource || 0} · мобов ${c.mob || 0} · добычи ${c.loot || 0} · игроков ${c.player || 0}` : "нет трафика",
          g.windows > 1 && `окон игры: ${g.windows} (бот берёт активное)`].filter(Boolean).join(" · "))}</span></div>
      <div class="bots-row">
        <button type="button" data-a="calibrate" title="Встаньте на открытое место: бот кликнет 4 раза рядом с персонажем">Калибровка</button>
        <span class="${g.calibrated ? "good" : "bad"}">${g.calibrated ? `откалиброван для окна ${esc(g.size)}` : `нужна калибровка для окна ${esc(g.size)}`}</span>
        <button type="button" data-a="test_click" title="Клик чуть ниже персонажа — проверка, что клики доходят до игры">Пробный клик</button>
      </div>`;
    if (box.dataset.html !== html) {
      box.dataset.html = html;
      box.innerHTML = html;
      $$("[data-a]", box).forEach((b) => b.addEventListener("click", async () => { await this.post({ action: b.dataset.a }); this.poll(); }));
    }
  },

  renderTasks(d) {
    const tasks = $("#bot-tasks");
    if (!tasks.dataset.ready) {
      tasks.dataset.ready = 1;
      tasks.innerHTML = Object.entries(d.tasks).map(([k, t]) =>
        `<label class="bot-task"><input type="radio" name="bot-task" value="${k}"> ${esc(t)}</label>`).join("");
      $$("input", tasks).forEach((r) => r.addEventListener("change", () => {
        this.task = r.value;
        this.formKey = "";
        this.post({ action: "settings", task: r.value });
        this.renderTasks(this.data);
      }));
    }
    $$("input", tasks).forEach((r) => { r.checked = r.value === this.task; r.disabled = d.bot.running; });
    const b = d.bot;
    const st = b.stats;
    const stats = [st.gathered && `собрано ${st.gathered}`, st.orders && `заказов ${st.orders}`, st.trips && `рейсов ${st.trips}`,
      st.kills && `убито ${st.kills}`, st.loots && `добыча ${st.loots}`, st.floors && `этаж ${st.floors}`,
      st.runs && `данжей ${st.runs}`].filter(Boolean).join(" · ");
    const status = $("#bot-status");
    status.textContent = (b.running && b.task ? `${d.tasks[b.task] || b.task}: ` : "") + b.status + (stats ? ` · ${stats}` : "");
    status.className = "bot-status " + (b.status === "ошибка" ? "bad" : b.running ? "good" : "muted");
    $("#bot-start").disabled = $("#bot-stop").disabled = false;
    $("#bot-help").innerHTML = BOT_HELP[this.task] || "";
    $("#bot-log").innerHTML = b.log.slice(-10).reverse()
      .map((x) => `<div>${new Date(x.ts * 1000).toLocaleTimeString("ru-RU")} — ${esc(x.text)}</div>`).join("");
    const key = this.task + "|" + JSON.stringify(d.places.map((p) => p.name)) + "|" + Object.keys(d.macros).join(",");
    if (this.formKey !== key) {
      this.formKey = key;
      this.renderForm(d);
    }
  },

  renderForm(d) {
    const form = $("#bot-form");
    const task = this.task;
    const sect = task === "wander" ? "gather" : task === "dungeon_run" ? "dungeon" : task;
    const c = d.tasks_config[sect] || {};
    const s = d.settings;
    const opt = (v, t, cur) => `<option value="${esc(v)}"${String(cur) === String(v) ? " selected" : ""}>${esc(t)}</option>`;
    const num = (k, t, min, max, step = 1, title = "") => `<label${title ? ` title="${esc(title)}"` : ""}>${t} <input type="number" data-c="${k}" value="${c[k] ?? ""}" min="${min}" max="${max}" step="${step}"></label>`;
    const txt = (k, t, ph = "") => `<label>${t} <input type="text" data-c="${k}" value="${esc(c[k] ?? "")}" placeholder="${esc(ph)}"></label>`;
    const chk = (k, t) => `<label><input type="checkbox" data-c="${k}"${c[k] ? " checked" : ""}> ${t}</label>`;
    const place = (k, t, empty) => `<label>${t} <select data-c="${k}">${opt("", empty, c[k])}${d.places.map((p) =>
      opt(p.name, `${p.name} (${p.zone_name || p.zone})`, c[k])).join("")}</select></label>`;
    const macro = (k, t, empty) => `<label>${t} <select data-c="${k}">${opt("", empty, c[k])}${Object.keys(d.macros).sort()
      .map((m) => opt(m, m, c[k])).join("")}</select></label>`;
    const area = (k, t, ph) => `<label class="col">${t}<textarea data-c="${k}" rows="2" placeholder="${esc(ph)}">${esc(c[k] || "")}</textarea></label>`;
    const group = (title, body, open = false) => `<details class="bot-group"${open ? " open" : ""}><summary>${title}</summary>${body}</details>`;
    const session = `<div class="bots-row"><label>работа, мин <input type="number" data-s="work_min" value="${s.work_min}" min="1" max="1440"></label>
      <label>отдых, мин <input type="number" data-s="rest_min" value="${s.rest_min}" min="0" max="1440"></label></div>`;
    let html = "";
    if (task === "gather") {
      html = `<div class="bots-row">${BOT_RES.map(([r, n]) => `<label><input type="checkbox" data-res="${r}"${(c.res || []).includes(r) ? " checked" : ""}> ${n}</label>`).join("")}</div>
        <div class="bots-row">${num("tier_min", "тир от", 1, 8)}${num("tier_max", "до", 1, 8)}${num("enchant_min", "зачарование от", 0, 4)}
          ${num("radius", "радиус от старта, м", 10, 500, 5)}${num("avoid_mobs", "обходить мобов, м", 0, 50, 1, "0 — не обходить")}
          ${num("max_nodes", "узлов за запуск", 0, 10000, 1, "0 — без ограничения; потом бот гуляет")}${chk("avoid_players", "уходить от враждебных игроков")}</div>
        ${group("Где искать и куда сдавать", `<div class="bots-row">${chk("use_heat", "идти туда, где ресурсы бывают (тепловая карта радара)")}
          ${num("respawn_min", "не возвращаться раньше, мин", 1, 240, 1, "время восстановления узлов")}</div>
          <div class="bots-row">${num("bag_slots", "сумка полна после предметов", 0, 500, 1, "0 — не следить (перегруз ловится всегда)")}
          ${place("home_place", "куда сдавать", "— не сдавать —")}${macro("deposit_macro", "как сдавать", "готовый шаблон (сундук)")}</div>`)}${session}`;
    } else if (task === "wander") {
      html = `<div class="bots-row">${num("radius", "радиус прогулки от старта, м", 10, 500, 5)}</div>${session}`;
    } else if (task === "market") {
      html = `<div class="bots-row">${place("place", "где стоять", "там, где персонаж сейчас")}
          <label>заказы <select data-c="side">${opt("sell", "на продажу", c.side)}${opt("buy", "на покупку", c.side)}</select></label>
          ${macro("macro", "действия", "готовый шаблон по точкам")}</div>
        <div class="bots-row">${area("items", "Предметы (id через пробел или с новой строки)", "T4_BAG T5_2H_BOW")}</div>
        <div class="bots-row">${num("undercut", "шаг цены", 0, 1e9, 1, "продажа — на столько дешевле лучшей, покупка — дороже")}${num("qty", "кол-во", 1, 9999)}
          ${num("interval_min", "пауза между заказами, мин от", 0.1, 600, 0.1)}${num("interval_max", "до", 0.1, 600, 0.1)}
          ${num("orders", "заказов всего", 0, 10000, 1, "0 — без ограничения")}</div>
        ${group("Свои заказы и бюджет", `<div class="bots-row">${chk("skip_own", "не дублировать свой лучший заказ")}
          ${chk("relist_outbid", "перебитый заказ — поставить новый")}${num("budget", "бюджет покупок, серебро", 0, 1e12, 1000, "0 — без ограничения")}</div>
          <p class="muted">Свои заказы программа видит, когда в игре открыта вкладка «Мои заказы» рынка.</p>`)}${session}`;
    } else if (task === "transport") {
      html = `<div class="bots-row">${place("load_place", "погрузка (город А)", "— выберите место —")}${macro("load_macro", "макрос погрузки", "без макроса")}</div>
        <div class="bots-row">${place("unload_place", "разгрузка (город Б)", "— выберите место —")}
          <label>в городе Б <select data-c="unload">${opt("macro", "выполнить макрос", c.unload)}${opt("market_sell", "выставить на продажу", c.unload)}</select></label>
          ${macro("unload_macro", "макрос разгрузки", "без макроса")}</div>
        <div class="bots-row">${area("sell_items", "Что продавать в городе Б (если «выставить на продажу»)", "T4_ORE T5_ORE")}</div>
        <div class="bots-row">${num("undercut", "шаг цены", 0, 1e9)}${num("qty", "кол-во в заказе", 1, 9999)}
          <label>зоны по пути <select data-c="safety">${Object.entries(d.safety).map(([k, t]) => opt(k, t, c.safety)).join("")}</select></label>
          ${txt("mount_key", "клавиша маунта", "a")}${chk("round_trip", "туда и обратно")}${num("trips", "рейсов", 0, 1000, 1, "0 — без ограничения")}</div>
        ${group("Игроки на пути", `<div class="bots-row">${chk("avoid_players", "объезжать игроков")}${num("player_radius", "ближе, м", 10, 150, 5)}
          ${num("avoid_min", "не ходить к тому выходу, мин", 1, 120)}${txt("escape_key", "клавиша зелья побега", "3")}</div>
          <div class="bots-row">${txt("friends", "свои (не угроза)", "Имя1, Имя2")}${num("danger_ip", "опасная сила снаряжения", 0, 2000, 50, "0 — не учитывать")}</div>`)}`;
    } else if (task === "dungeon" || task === "dungeon_run") {
      const kinds = Object.entries(d.portal_kinds).map(([k, t]) =>
        `<label><input type="checkbox" data-kind="${k}"${(c.portal_kinds || []).includes(k) ? " checked" : ""}> ${esc(t)}</label>`).join("");
      html = (task === "dungeon_run" ? `<div class="bots-row">${place("home_place", "сундук в городе", "— выберите место —")}
          ${macro("deposit_macro", "сдать добычу", "готовый шаблон по точкам")}${num("runs", "данжей за запуск", 0, 1000, 1, "0 — без ограничения")}</div>
        <div class="bots-row"><label>зоны поиска <select data-c="safety">${Object.entries(d.safety).map(([k, t]) => opt(k, t, c.safety)).join("")}</select></label>
          ${num("search_zones", "сколько ближайших зон", 1, 20)}${num("search_min", "искать в зоне, мин", 0.5, 120, 0.5)}</div>
        <div class="bots-row"><span class="muted">порталы:</span> ${kinds}</div>` : "") + `
        <div class="bots-row">${chk("avoid_players", "уходить от игроков")}${num("player_radius", "ближе, м", 10, 150, 5)}
          ${num("max_floors", "этажей не больше", 1, 30)}</div>
        <div class="bots-row">${txt("exit_key", "клавиша быстрого выхода", "a")}
          ${num("exit_channel", "задержка выхода, с", 1, 60, 1, "урон сбивает выход: бот сначала добивает мобов рядом")}
          <span class="muted">быстрый выход переносит к входному порталу; не задана — выход пешком по этажам</span></div>
        <div class="bots-row"><label class="col">Умения <input type="text" data-c="skills" value="${esc(c.skills || "")}" placeholder="q:3 w:10@open e:20@boss 2:30@hp<40@self"></label>
          ${num("attack_range", "дальность атаки, м", 3, 40)}</div>
        <pre class="muted bots-help">${esc(d.skills_help)}</pre>
        <div class="bots-row">${txt("potion_key", "клавиша зелья", "2")}${num("potion_hp", "пить при HP ниже, %", 1, 99)}
          ${num("retreat_hp", "отходить при HP ниже, %", 0, 99)}${chk("retreat_exit", "при этом — быстрый выход")}</div>
        <div class="bots-row">${chk("loot_bags", "собирать добычу с мобов")}${chk("open_chests", "открывать сундуки")}
          ${num("chest_wait", "открытие сундука, с", 1, 60)}${num("explore_min", "искать новое, мин", 0.5, 60, 0.5)}${num("max_min", "всего, мин", 1, 600)}</div>
        ${group("Бой без лишнего риска", `<div class="bots-row">${num("max_pack", "не брать групп больше", 0, 20, 1, "0 — без ограничения")}
          ${num("rest_hp", "отдыхать между группами при HP ниже, %", 0, 99)}${num("max_mob_tier", "мобы не выше тира", 0, 8, 1, "0 — любые")}
          ${chk("skip_elite", "пропускать элитных (кроме босса)")}</div>
          <div class="bots-row">${chk("kite", "отступать (дальний бой)")}${num("kite_dist", "если моб ближе, м", 1, 15)}</div>`)}
        ${group("Угрозы и побег", `<div class="bots-row">${txt("friends", "свои (не угроза)", "Имя1, Имя2")}
          ${num("danger_ip", "опасная сила снаряжения", 0, 2000, 50, "0 — не учитывать")}</div>
          <div class="bots-row">${txt("escape_key", "клавиша зелья побега", "3")}${txt("mount_key", "клавиша маунта (побег в открытом мире)", "a")}</div>`)}
        ${task === "dungeon_run" ? group("Порталы, сумка, ремонт и докупка", `<div class="bots-row">${num("portal_enchant_min", "зачарование портала от", 0, 4)}
          ${num("portal_enchant_max", "до", 0, 4)}${num("bag_slots", "сумка полна после предметов", 0, 500, 1, "0 — не следить (перегруз ловится всегда)")}</div>
          <div class="bots-row">${place("repair_place", "ремонт: где", "— на месте —")}${macro("repair_macro", "макрос", "— нет —")}${num("repair_every", "каждые N данжей", 0, 100)}</div>
          <div class="bots-row">${place("restock_place", "докупка: где", "— на месте —")}${macro("restock_macro", "макрос", "— нет —")}${num("restock_every", "каждые N данжей", 0, 100)}</div>`) : ""}`;
    } else if (task === "schedule") {
      html = `<div class="bots-row"><label class="col">Расписание<textarea data-sched rows="6" spellcheck="false">${esc(s.schedule || "")}</textarea></label></div>
        <pre class="muted bots-help">${esc(d.schedule_help)}</pre>
        <p class="muted">Настройки каждой задачи — те, что заданы на её вкладке выше.</p>`;
    }
    form.innerHTML = html;
    const send = (patch) => this.post({ action: "configure", [sect]: patch });
    $$("[data-c]", form).forEach((inp) => inp.addEventListener("change", () => {
      send({ [inp.dataset.c]: inp.type === "checkbox" ? inp.checked : inp.type === "number" ? Number(inp.value) : inp.value });
    }));
    $$("[data-res]", form).forEach((inp) => inp.addEventListener("change", () => {
      send({ res: $$("[data-res]", form).filter((x) => x.checked).map((x) => x.dataset.res) });
    }));
    $$("[data-kind]", form).forEach((inp) => inp.addEventListener("change", () => {
      send({ portal_kinds: $$("[data-kind]", form).filter((x) => x.checked).map((x) => x.dataset.kind) });
    }));
    $$("[data-sched]", form).forEach((inp) => inp.addEventListener("change", async () => {
      if (await this.post({ action: "settings", schedule: inp.value }, "расписание сохранено")) this.formKey = "";
    }));
    $$("[data-s]", form).forEach((inp) => inp.addEventListener("change", () => this.post({ action: "settings", [inp.dataset.s]: Number(inp.value) })));
  },

  renderExtras(d) {
    const check = $("#bot-check");
    const items = d.checklist || [];
    const html = items.map((c) => `<li class="${c.ok ? "good" : c.required ? "bad" : "muted"}">${c.ok ? "✓" : c.required ? "✗" : "•"} ${esc(c.item)}${c.hint ? ` — <span class="muted">${esc(c.hint)}</span>` : ""}</li>`).join("");
    if (check.dataset.html !== html) { check.dataset.html = html; check.innerHTML = html; }
    const pick = $("#profile-pick");
    const plist = (d.profiles || []).join("\n");
    if (pick.dataset.list !== plist) {
      pick.dataset.list = plist;
      pick.innerHTML = (d.profiles || []).length ? d.profiles.map((p) => `<option>${esc(p)}</option>`).join("") : `<option value="">— нет —</option>`;
    }
    const s = d.bot.session;
    const hours = (v) => (v == null ? "—" : fmt(v));
    const cur = s ? `<p><b>Текущий запуск</b> (${esc(d.tasks[s.task] || s.task)}, ${fmt1(s.minutes)} мин): серебро ${fmt(s.silver)} (${hours(s.silver_hour)}/ч),
      слава ${fmt(s.fame)} (${hours(s.fame_hour)}/ч)${s.runs ? `, данжей ${s.runs} (${fmt1(s.min_per_run)} мин на данж)` : ""}${s.deaths ? `, гибелей ${s.deaths}` : ""}</p>` : "";
    const rows = (d.history || []).slice(0, 15).map((h) => `<tr><td>${dateTime(h.start)}</td><td>${esc(d.tasks[h.task] || h.task)}</td>
      <td>${fmt1(h.minutes)}</td><td>${fmt(h.silver)}</td><td>${hours(h.silver_hour)}</td><td>${fmt(h.fame)}</td>
      <td>${h.runs || h.gathered || h.orders || h.trips || "—"}</td><td>${h.deaths || ""}</td><td class="muted">${esc(h.status)}</td></tr>`).join("");
    const st = cur + (rows ? `<table class="bot-points"><tr><th>Начало</th><th>Задача</th><th>мин</th><th>серебро</th><th>в час</th><th>слава</th>
      <th>итог</th><th>гибели</th><th>статус</th></tr>${rows}</table>` : `<p class="muted">Запусков пока не было.</p>`);
    const box = $("#bot-stats");
    if (box.dataset.html !== st) { box.dataset.html = st; box.innerHTML = st; }
  },

  renderPoints(d) {
    const rec = d.recording;
    const g = d.game;
    $("#bot-points-size").textContent = d.points_size ? `Указаны в окне ${d.points_size}` +
      (g && g.size && g.size !== d.points_size ? ` — сейчас окно ${g.size}, укажите заново.` : ".") : "";
    const html = `<table class="bot-points">${d.points.map((p) => `<tr><td>${esc(p.label)}</td>
      <td class="muted">${rec && rec.point === p.name ? "<b>кликните в окне игры…</b>" : p.value ? `${p.value[0].toFixed(3)}, ${p.value[1].toFixed(3)}` : "не указана"}</td>
      <td>${rec && rec.point === p.name ? `<button type="button" data-cancel>Отмена</button>` : `<button type="button" data-cap="${p.name}">Указать</button>`}
        ${p.value ? `<button type="button" data-del="${p.name}" title="Забыть точку">×</button>` : ""}</td></tr>`).join("")}</table>`;
    const box = $("#bot-points");
    if (box.dataset.html === html) return;
    box.dataset.html = html;
    box.innerHTML = html;
    $$("[data-cap]", box).forEach((b) => b.addEventListener("click", async () => { await this.post({ action: "capture_point", name: b.dataset.cap }); this.poll(); }));
    $$("[data-del]", box).forEach((b) => b.addEventListener("click", async () => { await this.post({ action: "delete_point", name: b.dataset.del }); this.poll(); }));
    $$("[data-cancel]", box).forEach((b) => b.addEventListener("click", async () => { await this.post({ action: "cancel_capture" }); this.poll(); }));
  },

  renderPlaces(d) {
    const html = d.places.length ? `<table class="bot-points">${d.places.map((p) => `<tr><td><b>${esc(p.name)}</b></td>
      <td class="muted">${esc(p.zone_name || p.zone)} (${Math.round(p.x)}, ${Math.round(p.y)})</td>
      <td><button type="button" data-delp="${esc(p.name)}" title="Удалить место">×</button></td></tr>`).join("")}</table>`
      : `<p class="muted">Мест пока нет.</p>`;
    const box = $("#bot-places");
    if (box.dataset.html === html) return;
    box.dataset.html = html;
    box.innerHTML = html;
    $$("[data-delp]", box).forEach((b) => b.addEventListener("click", async () => {
      if (confirm(`Удалить место «${b.dataset.delp}»?`)) { await this.post({ action: "delete_place", name: b.dataset.delp }); this.formKey = ""; this.poll(); }
    }));
  },

  renderMacros(d) {
    $("#macro-help").textContent = d.macro_help;
    const tpl = $("#macro-template");
    if (tpl.options.length < 2) {
      tpl.innerHTML = `<option value="">—</option>` + Object.keys(d.templates).map((k) =>
        `<option value="${k}">${{ market_sell: "рынок: продажа", market_buy: "рынок: покупка", loot_all: "взять всё", stash_deposit: "сундук: положить всё" }[k] || k}</option>`).join("");
    }
    const macros = Object.keys(d.macros).sort();
    const pick = $("#macro-pick");
    if (pick.dataset.list !== macros.join("\n")) {
      pick.dataset.list = macros.join("\n");
      pick.innerHTML = `<option value="">— новый —</option>` + macros.map((m) => `<option>${esc(m)}</option>`).join("");
    }
    pick.value = this.picked;
    const rec = d.recording;
    $("#macro-rec").textContent = rec && !rec.point ? "■ Стоп" : "● Запись кликов";
  },
});
