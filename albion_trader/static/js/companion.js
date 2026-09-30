"use strict";
// Окно-компаньон: только инструменты, компактная раскладка, «поверх игры».
document.body.classList.add("companion");
App.start().then(() => App.startAutoRefresh(20000)).catch((e) => {
  document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="bad">Не удалось загрузить: ${esc(e.message)}</p>`);
});
refreshConn();
setInterval(refreshConn, 15000);

(() => {
  const box = $("#pin");
  const hint = $("#pin-hint");
  const saved = loadSettings("albion-trader-companion");
  box.checked = !!saved.topmost;
  const apply = async () => {
    saveSettings({ ...loadSettings("albion-trader-companion"), topmost: box.checked }, "albion-trader-companion");
    try {
      const r = await apiPost("/api/window", { topmost: box.checked });
      hint.textContent = box.checked && !r.applied ? (r.reason || "") : "";
    } catch (e) { hint.textContent = box.checked ? "закрепление доступно на компьютере с программой" : ""; }
  };
  box.addEventListener("change", apply);
  if (box.checked) setTimeout(apply, 800);   // окну нужно время, чтобы появиться
})();
