"use strict";
// Вкладка «Мир → Радар»: карта, настройки (общие с окном радара), игроки рядом,
// история встреч, запись и диагностика кодов событий.

App.tab({
  id: "radar", group: "world", title: "Радар",
  init(el) {
    const o = radarOptions();
    const check = (k, t, title = "") => `<label${title ? ` title="${esc(title)}"` : ""}><input type="checkbox" data-opt="${k}"${o[k] ? " checked" : ""}> ${t}</label>`;
    const select = (k, t, opts) => `<label>${t} <select data-opt="${k}">${opts.map(([v, n]) =>
      `<option value="${v}"${String(o[k]) === v ? " selected" : ""}>${n}</option>`).join("")}</select></label>`;
    const text = (k, t) => `<label>${t} <input type="text" data-opt="${k}" value="${esc(o[k] || "")}"></label>`;
    const num = (k, t, min, max, step = 1) => `<label>${t} <input type="number" data-opt="${k}" min="${min}" max="${max}" step="${step}" value="${o[k]}"></label>`;
    const area = (k, t, ph) => `<label class="col">${t}<textarea data-opt="${k}" rows="2" placeholder="${esc(ph)}">${esc(o[k] || "")}</textarea></label>`;
    const layer = (k) => `<label><input type="checkbox" data-opt="${k.kind}"${o[k.kind] ? " checked" : ""}>
      <span class="radar-dot" style="background:${k.color}"></span>${k.title}</label>`;
    const matrix = `<table class="radar-matrix"><tr><th></th>${[1, 2, 3, 4, 5, 6, 7, 8].map((t) => `<th>${t}</th>`).join("")}</tr>
      ${RES_KINDS.map(([r, n]) => `<tr><th>${n}</th>${[1, 2, 3, 4, 5, 6, 7, 8].map((t) =>
        `<td><input type="checkbox" data-res="${r}" data-tier="${t}"${(o.resmatrix[r] || {})[t] === false ? "" : " checked"}></td>`).join("")}</tr>`).join("")}</table>`;
    el.innerHTML = `
      <p class="muted intro">Объекты вокруг персонажа из трафика игры на фоне схемы текущей зоны (или своей картинки
        карты из <code>data/maps</code>). Настройки действуют и здесь, и в отдельном окне радара.</p>
      <div class="radar">
        <div class="radar-map" tabindex="0"><canvas id="radar-canvas"></canvas><div class="radar-hud" id="radar-hud"></div></div>
        <div class="radar-side">
          <div class="radar-actions">
            <button type="button" id="radar-open">Окно радара</button>
            <label title="Держать окно радара поверх игры (Windows)"><input type="checkbox" id="radar-pin"${o.pin ? " checked" : ""}> поверх игры</label>
          </div>
          <div class="radar-actions" id="radar-overlay">
            <label title="Окно радара полупрозрачное и пропускает клики в игру (Windows, экспериментально)">
              <input type="checkbox" id="radar-ov"${o.overlay ? " checked" : ""}> оверлей</label>
            <input type="range" id="radar-ov-alpha" min="30" max="100" value="${o.overlayalpha || 75}" title="Непрозрачность окна радара">
          </div>
          <span class="muted" id="radar-pin-hint"></span>
          <div class="radar-profiles" id="radar-profiles"></div>
          ${select("mode", "Карта", [["follow", "за персонажем"], ["static", "статичная, вся зона"]])}
          <label data-show="follow">Масштаб <input type="range" min="1" max="20" step="0.5" data-opt="zoom" value="${o.zoom}"></label>
          <label data-show="static">Приближение <input type="range" min="1" max="4" step="0.25" data-opt="staticzoom" value="${o.staticzoom}"></label>
          <div class="radar-layers">${RADAR_KINDS.map(layer).join("")}</div>
          <div class="radar-layers">
            ${check("hostile", '<span class="radar-dot" style="background:#ff3b3b"></span>враждебные')}
            ${check("factional", '<span class="radar-dot" style="background:#f5a524"></span>фракция')}
            ${check("passive", '<span class="radar-dot" style="background:#9be29b"></span>мирные')}
          </div>
          <div class="radar-layers">
            ${check("alert", "оповещать о враждебных")} ${check("route", "маршрут сбора")}
            ${check("heat", "тепловая карта")} ${check("trails", "следы")}
          </div>
          <details class="radar-more"><summary>Игроки и оповещения</summary>
            ${select("playerlabel", "Подпись", [["name", "имя"], ["guild", "имя и гильдия"], ["power", "имя, сила, роль"], ["none", "без подписи"]])}
            ${text("myguild", "Моя гильдия")}
            ${text("myalliance", "Мой альянс")}
            ${area("friends", "Друзья (свои)", "имена через запятую или с новой строки")}
            ${area("ignore", "Скрывать", "имена через запятую или с новой строки")}
            ${num("alertradius", "Радиус оповещения, м", 10, 300, 5)}
            ${check("alertsound", "Звук")}
            ${check("alertnotify", "Оповещение программы (Windows, Telegram, Discord)", "Правило «Радар: враждебный игрок» во вкладке «Оповещения»")}
            ${check("squads", "Отряды (4+ враждебных рядом)")}
            ${check("arrows", "Стрелки к объектам за краем карты")}
            ${check("stale", "Бледными — игроки и мобы без событий 20 с")}
          </details>
          <details class="radar-more"><summary>Ресурсы</summary>
            ${matrix}
            ${num("mintier", "Мин. тир", 1, 8)}
            ${select("minenchant", "Мин. зачарование", [["0", "любое"], ["1", ".1+"], ["2", ".2+"], ["3", ".3+"], ["4", ".4"]])}
            ${num("minvalue", "Мин. цена узла, серебро", 0, 10000000, 1000)}
            ${select("resstyle", "Показывать", [["icon", "значок"], ["text", "надпись"], ["both", "значок и надпись"]])}
            ${select("reslabel", "Подпись значка", [["tier", "тир.зачарование"], ["value", "цена узла"], ["size", "запас"]])}
            ${select("iconset", "Значки", [["drawn", "свои (без интернета)"], ["game", "из игры (render.albiononline.com)"]])}
            ${num("routelen", "Узлов в маршруте", 2, 30)}
            ${check("depleted", "Истощённые узлы и таймер респауна")}
            ${num("respawnmin", "Респаун через, мин", 1, 120)}
          </details>
          <details class="radar-more"><summary>Мобы</summary>
            ${num("mobmintier", "Мин. тир", 1, 8)}
            ${check("bossesonly", "Только боссы и чемпионы")}
            ${check("livingasres", "Живые ресурсы (шкуры и т. п.) — значком ресурса")}
            ${check("moblabels", "Названия и HP мобов")}
            <label title="Номер моба в событии + сдвиг = строка в mobs.json. Подберите, пока названия в подсказке не совпадут с игрой">
              Сдвиг номера моба <input type="number" id="radar-mob-offset" step="1" style="width:80px"></label>
          </details>
          <details class="radar-more"><summary>Карта</summary>
            ${check("rotate", "Поворот 45° (как камера игры)")}
            ${check("background", "Фон — схема зоны или своя картинка")}
            <label>Яркость фона <input type="range" min="10" max="100" data-opt="bgopacity" value="${o.bgopacity}"></label>
            ${check("exits", "Выходы из зоны")}
            ${check("labels", "Подписи")}
            <p class="muted small-hint">Своя картинка карты: <code>data/maps/&lt;id зоны&gt;.png</code> (как карта зоны в игре —
              ромбом; для вида сверху без поворота рядом <code>&lt;id&gt;.json</code> с <code>{"kind": "flat"}</code>).</p>
          </details>
          <p class="muted small-hint">Клавиши (на карте или в окне радара): <b>+</b>/<b>−</b> масштаб, <b>M</b> вид, <b>L</b> подписи,
            <b>B</b> фон, <b>R</b> поворот, <b>H</b> тепловая карта, <b>T</b> следы, <b>1–5</b> слои, <b>P</b> профиль.
            Наведите курсор на объект — подробности.</p>
          <h2>Рядом</h2>
          <div class="radar-list" id="radar-list"></div>
        </div>
      </div>
      <h2>Игроки рядом</h2>
      <div id="radar-players"></div>
      <details class="radar-codes" id="radar-history-box"><summary>История встреч</summary>
        <div id="radar-history"></div>
      </details>
      <details class="radar-codes"><summary>Запись: перемотка</summary>
        <p class="muted">Воспроизведение записи трафика (.pcap — например, сделанной с <code>serve --record</code>) на радаре:
          пауза, скорость, перемотка. Пока запись открыта, радар показывает её вместо игры.</p>
        <div class="radar-replay" id="radar-replay"></div>
      </details>
      <details class="radar-codes"><summary>Коды событий (диагностика)</summary>
        <div id="radar-suggest"></div>
        <p class="muted">Все события, что пришли от сервера: код, имя (если известно), количество и параметры с типами.
          <code>·pos</code> — значение похоже на координаты.</p>
        <div id="radar-codes"></div>
        <h4>Запросы игры (свои действия)</h4>
        <div id="radar-requests"></div>
      </details>`;
    this.view = new RadarView($("#radar-canvas", el), { hud: $("#radar-hud", el) });
    this.view.keysOnlyWhen = () => App.current === this;
    this.view.bindKeys(window);
    this.view.onchange = () => this.syncInputs(el);
    const modeRows = () => $$("[data-show]", el).forEach((r) => { r.hidden = r.dataset.show !== this.view.opts.mode; });
    this.syncInputs = (root) => {
      const s = radarOptions();
      $$("[data-opt]", root).forEach((inp) => {
        const v = s[inp.dataset.opt];
        if (inp.type === "checkbox") inp.checked = !!v; else if (document.activeElement !== inp) inp.value = v ?? "";
      });
      modeRows();
      this.renderProfiles();
    };
    $$("[data-opt]", el).forEach((inp) => inp.addEventListener(inp.type === "text" || inp.tagName === "TEXTAREA" ? "change" : "input", () => {
      const v = inp.type === "checkbox" ? inp.checked
        : (inp.type === "range" || inp.type === "number" || inp.dataset.opt === "minenchant") ? Number(inp.value) : inp.value;
      this.view.update({ [inp.dataset.opt]: v, profile: "" });
      this.renderList();
    }));
    $$("[data-res]", el).forEach((inp) => inp.addEventListener("change", () => {
      const m = { ...(radarOptions().resmatrix || {}) };
      m[inp.dataset.res] = { ...(m[inp.dataset.res] || {}), [inp.dataset.tier]: inp.checked };
      this.view.update({ resmatrix: m, profile: "" });
    }));
    modeRows();
    $("#radar-open", el).addEventListener("click", () => openWindow("radar.html", "albion-radar", 520, 520, "radar"));
    const hint = (t) => { $("#radar-pin-hint").textContent = t || ""; };
    $("#radar-pin", el).addEventListener("change", async (ev) => {
      const on = ev.target.checked;
      this.view.update({ pin: on });
      try {
        const r = await apiPost("/api/window", { which: "radar", topmost: on });
        hint(on && !r.applied ? r.reason : "");
      } catch { hint(on ? "доступно на компьютере с программой" : ""); }
    });
    const overlay = async () => {
      const on = $("#radar-ov").checked, alpha = Number($("#radar-ov-alpha").value);
      this.view.update({ overlay: on, overlayalpha: alpha });
      try {
        const r = await apiPost("/api/window", { which: "radar", overlay: on, alpha });
        hint(on && !r.overlay_applied ? r.reason : on ? "окно пропускает клики — выключите оверлей здесь, чтобы снова им управлять" : "");
      } catch { hint(on ? "доступно на компьютере с программой" : ""); }
    };
    $("#radar-ov", el).addEventListener("change", overlay);
    $("#radar-ov-alpha", el).addEventListener("change", overlay);
    const offset = $("#radar-mob-offset", el);
    offset.addEventListener("change", async () => {
      await apiPost("/api/radar/codes", { mob_offset: Number(offset.value) || 0 });
      this.poll();
    });

    this.players = makeTable($("#radar-players", el), [
      { key: "name", title: "Игрок", html: (r) => `<b>${esc(r.name)}</b>` + (r.guild || r.alliance
        ? ` <span class="muted">${r.guild ? `[${esc(r.guild)}]` : ""}${r.alliance ? ` &lt;${esc(r.alliance)}&gt;` : ""}</span>` : "") },
      { key: "status", title: "Статус", sort: (r) => r._st.key,
        html: (r) => `<span class="radar-dot" style="background:${r._st.color}"></span> ${esc(r._st.text)}` },
      { key: "ip", title: "Сила", html: (r) => (r.ip ? fmt(r.ip) : "—") },
      { key: "role", title: "Роль", html: (r) => esc(r.role || "—") },
      { key: "hp", title: "HP", sort: (r) => (r.max_health ? r.health / r.max_health : -1),
        html: (r) => (r.max_health && r.health != null ? `${Math.round(100 * r.health / r.max_health)}%` : "—") },
      { key: "mounted", title: "♞", sort: (r) => (r.mounted ? 1 : 0), html: (r) => (r.mounted ? "♞" : "") },
      { key: "weapon", title: "Оружие", sort: (r) => r._weapon,
        html: (r) => `<span title="${esc((r.equipment || []).map((i) => `${i.slot}: ${i.name || i.id}`).join("\n"))}">${esc(r._weapon || "—")}</span>` },
      { key: "kb", title: "Убийств/смертей", sort: (r) => (r.kb ? r.kb.kills : -1),
        html: (r) => (r.kb ? `${fmt(r.kb.kills)} / ${fmt(r.kb.deaths)}` : "—") },
      { key: "dist", title: "м", html: (r) => fmt(r.dist) },
    ], { sort: "dist", asc: true, empty: "Игроков рядом нет." });
    this.history = makeTable($("#radar-history", el), [
      { key: "name", title: "Игрок", html: (r) => `<b>${esc(r.name)}</b>` + (r.guild ? ` <span class="muted">[${esc(r.guild)}]</span>` : "") },
      { key: "alliance", title: "Альянс", html: (r) => esc(r.alliance || "—") },
      { key: "times", title: "Встреч", html: (r) => fmt(r.times) },
      { key: "hostile", title: "Враждебен", html: (r) => (r.hostile ? `<span class="bad">${fmt(r.hostile)}</span>` : "—") },
      { key: "kb", title: "Убийств/смертей", sort: (r) => (r.kb ? r.kb.kills : -1),
        html: (r) => (r.kb ? `${fmt(r.kb.kills)} / ${fmt(r.kb.deaths)}` : "—") },
      { key: "zones", title: "Зоны", sort: (r) => r.zones.length, html: (r) => esc(r.zone_names.join(", ")) },
      { key: "last_seen", title: "Последний раз", html: (r) => dateTime(r.last_seen) },
    ], { sort: "last_seen", asc: false, empty: "Пока никого не встречали." });

    const frame = () => { if (App.current === this) this.view.draw(); requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
    setInterval(() => { if (App.current === this && !document.hidden) this.poll(); }, 300);
    setInterval(() => {
      if (App.current === this && !document.hidden) { this.renderCodes(); this.renderPlayers(); this.view.loadHeat(); }
    }, 1500);
    setInterval(() => { if (App.current === this && !document.hidden && $("#radar-history-box").open) this.loadHistory(); }, 30000);
    $("#radar-history-box", el).addEventListener("toggle", () => this.loadHistory());
    this.initReplay($("#radar-replay", el));
    this.poll().then(() => { this.renderPlayers(); offset.value = this.view.data.mob_offset ?? 0; });
    this.renderProfiles();
  },
  show() { if (this.view) this.poll(); },

  async poll() {
    await this.view.poll();
    this.renderList();
  },

  async loadHistory() {
    try { this.history.set((await api("/api/radar/history", { days: 30 })).rows); } catch { /* нет связи */ }
  },

  // Профили: встроенные «Фарм», «PvP», «Сбор» и свои сохранённые.
  renderProfiles() {
    const box = $("#radar-profiles");
    if (!box) return;
    const saved = loadSettings("albion-trader-radar-profiles");
    const all = { ...DEFAULT_PROFILES, ...saved };
    const cur = this.view.opts.profile;
    box.innerHTML = Object.keys(all).map((n) => `<button type="button" class="chip${n === cur ? " active" : ""}" data-p="${esc(n)}">${esc(n)}${saved[n] && !DEFAULT_PROFILES[n] ? ' <span data-del="1" title="удалить">×</span>' : ""}</button>`).join("")
      + '<button type="button" class="chip" id="radar-psave" title="Сохранить текущие настройки как профиль">+ сохранить</button>';
    $$("[data-p]", box).forEach((b) => b.addEventListener("click", (ev) => {
      const name = b.dataset.p;
      if (ev.target.dataset.del) {
        const s = loadSettings("albion-trader-radar-profiles"); delete s[name];
        saveSettings(s, "albion-trader-radar-profiles"); this.renderProfiles(); return;
      }
      this.view.update({ ...all[name], profile: name });
    }));
    $("#radar-psave").addEventListener("click", () => {
      const name = (prompt("Название профиля") || "").trim();
      if (!name) return;
      const o = radarOptions();
      const keep = Object.fromEntries(Object.entries(o).filter(([k]) => !["pin", "overlay", "overlayalpha", "profile"].includes(k)));
      saveSettings({ ...loadSettings("albion-trader-radar-profiles"), [name]: keep }, "albion-trader-radar-profiles");
      this.view.update({ profile: name });
    });
  },

  renderPlayers() {
    const me = this.view.data.me, o = this.view.opts;
    this.players.set(this.view.data.entities.filter((e) => e.kind === "player" && radarVisible(e, o, me))
      .map((e) => ({ ...e, _st: playerStatus(e, me, o),
        _weapon: ((e.equipment || []).find((i) => i.slot === "оружие") || {}).name || "" })));
  },

  renderList() {
    const o = this.view.opts, me = this.view.data.me;
    const rows = this.view.data.entities.filter((e) => e.kind !== "player" && radarVisible(e, o, me)).slice(0, 60);
    $("#radar-list").innerHTML = rows.length ? rows.map((e) =>
      `<div><span class="radar-dot" style="background:${e.kind === "resource" ? (TIER_COLOR[e.tier] || RADAR_COLOR.resource)
        : e.kind === "loot" && e.rarity != null ? CHEST_COLOR[e.rarity] : RADAR_COLOR[e.kind]}"></span>
        <span>${e.kind === "object" || e.kind === "loot" ? objectInfo(e).icon + " " : ""}${esc(radarLabel(e))}${e.value ? ` <span class="muted">≈${fmtShort(e.value)}</span>` : ""}</span>
        <span class="muted">${fmt(e.dist)} м</span></div>`).join("")
      : '<p class="muted">Никого. Смените зону, чтобы сборщик увидел объекты вокруг.</p>';
  },

  renderCodes() {
    const d = this.view.data, codes = d.codes || [], sugg = d.suggestions || [];
    const changed = Object.entries(d.codes_changed || {});
    const reset = changed.length
      ? `<div class="bad">Номера изменены относительно встроенных (data\\opcodes.json): ${changed.map(([k, [cur, def]]) =>
          `${esc(k.replace(/^(event|op):/, ""))} ${cur} (было ${def})`).join(", ")}.
          <button type="button" id="radar-reset-codes">Вернуть номера по умолчанию</button></div>`
      : "";
    $("#radar-suggest").innerHTML = reset + `<label class="inline"><input type="checkbox" id="radar-auto"${d.autocodes ? " checked" : ""}>
        Исправлять номера событий автоматически</label>`
      + (sugg.length ? `<p>Похоже, номера событий на этом сервере другие:</p><ul>${sugg.map((s) =>
        `<li><b>${esc(s.name)}</b>: приходит под кодом <b>${s.code}</b> (${s.samples} раз, ${Math.round(s.share * 100)}%),
          настроен ${s.current ?? "—"} <button type="button" data-apply="${esc(s.name)}" data-code="${s.code}">Применить</button></li>`).join("")}</ul>`
        : '<p class="muted">Расхождений в номерах событий не найдено.</p>');
    $("#radar-auto").addEventListener("change", (ev) => apiPost("/api/radar/codes", { auto: ev.target.checked }));
    const resetBtn = $("#radar-reset-codes");
    if (resetBtn) resetBtn.addEventListener("click", async () => {
      if (!confirm("Вернуть встроенные номера событий и операций? Файл data\\opcodes.json сохранится как opcodes.json.bak.")) return;
      await apiPost("/api/radar/codes", { reset: true });
      this.poll();
    });
    $$("[data-apply]", $("#radar-suggest")).forEach((b) => b.addEventListener("click", async () => {
      await apiPost("/api/radar/codes", { apply: { [b.dataset.apply]: Number(b.dataset.code) } });
      this.poll();
    }));
    $("#radar-codes").innerHTML = codes.length ? `<div class="table-wrap"><table><thead><tr><th>Код</th><th>Имя</th>
      <th>Кол-во</th><th>Параметры</th></tr></thead><tbody>${codes.map((c) => `<tr><td>${c.code}</td>
      <td>${esc(c.name || "—")}</td><td>${fmt(c.count)}</td><td><code>${esc(c.shape)}</code></td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">Событий пока не было.</p>';
    const rq = d.requests || { total: 0, codes: [] };
    const moveLine = !rq.total
      ? '<p class="bad">Запросов от игры не видно — программа не видит исходящий трафик, поэтому своя позиция не обновляется. Запустите программу от администратора; если не поможет — пришлите data\\radar_check.txt.</p>'
      : rq.move_seen
        ? `<p class="good">Своё движение: запрос с кодом ${rq.move}${rq.detected ? " (опознан автоматически)" : ""}.</p>`
        : '<p class="bad">Запрос движения пока не найден — пройдитесь персонажем несколько шагов. Если не появится — пришлите эту таблицу.</p>';
    $("#radar-requests").innerHTML = moveLine + (rq.codes.length ? `<div class="table-wrap"><table><thead><tr><th>Код</th><th>Имя</th>
      <th>Кол-во</th><th>С позицией</th><th>Параметры</th></tr></thead><tbody>${rq.codes.map((c) => `<tr><td>${c.code}</td>
      <td>${esc(c.name || "—")}</td><td>${fmt(c.count)}</td><td>${fmt(c.positions)}</td><td><code>${esc(c.shape)}</code></td></tr>`).join("")}
      </tbody></table></div>` : "");
  },

  // Перемотка записи: список .pcap, воспроизведение, пауза, скорость, ползунок.
  initReplay(box) {
    const render = async () => {
      let st;
      try { st = await api("/api/radar/replay"); } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
      const files = st.files || [];
      if (!st.active) {
        box.innerHTML = files.length ? `<label>Запись <select id="rp-file">${files.map((f) =>
          `<option value="${esc(f.name)}">${esc(f.name)} (${fmt(f.size / 1024)} КБ)</option>`).join("")}</select></label>
          <button type="button" id="rp-open">Открыть</button>`
          : '<p class="muted">Записей нет. Запустите программу с <code>serve --record data/radar.pcap</code> или положите .pcap в папку data.</p>';
        const b = $("#rp-open", box);
        if (b) b.addEventListener("click", async () => { await apiPost("/api/radar/replay", { open: $("#rp-file", box).value }); render(); });
        return;
      }
      box.innerHTML = `<div class="radar-actions">
          <button type="button" data-a="${st.playing ? "pause" : "play"}">${st.playing ? "❚❚ пауза" : "▶ играть"}</button>
          <select id="rp-speed">${[0.5, 1, 2, 4, 8, 16].map((s) => `<option value="${s}"${s === st.speed ? " selected" : ""}>×${s}</option>`).join("")}</select>
          <input type="range" id="rp-pos" min="0" max="${Math.ceil(st.duration)}" step="1" value="${Math.floor(st.pos)}" style="flex:1">
          <span class="muted" id="rp-time">${fmt(st.pos)} / ${fmt(st.duration)} с</span>
          <button type="button" data-a="close">✕ к игре</button></div>
        <p class="muted">${esc(st.file)}: пакетов ${fmt(st.packets)}</p>`;
      $$("[data-a]", box).forEach((b) => b.addEventListener("click", async () => {
        await apiPost("/api/radar/replay", { action: b.dataset.a }); render();
      }));
      $("#rp-speed", box).addEventListener("change", (ev) => apiPost("/api/radar/replay", { speed: Number(ev.target.value) }));
      $("#rp-pos", box).addEventListener("change", (ev) => apiPost("/api/radar/replay", { seek: Number(ev.target.value) }));
    };
    box.parentElement.addEventListener("toggle", render);
    setInterval(() => {
      const r = this.view.data.replay;
      if (!r || !box.parentElement.open) return;
      const pos = $("#rp-pos", box), t = $("#rp-time", box);
      if (pos && document.activeElement !== pos) pos.value = Math.floor(r.pos);
      if (t) t.textContent = `${fmt(r.pos)} / ${fmt(r.duration)} с`;
    }, 1000);
  },
});
