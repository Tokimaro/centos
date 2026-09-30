"use strict";
// Окно радара: только карта и значки. Настройки — на вкладке «Мир → Радар» (общие через localStorage).
(() => {
  const view = new RadarView($("#radar-canvas"), { compact: true });
  const status = $("#radar-status");
  const poll = async () => {
    await view.poll();
    status.hidden = !view.error;
    status.textContent = view.error ? `нет связи с программой: ${view.error}` : "";
  };
  const frame = () => { if (!document.hidden) view.draw(); requestAnimationFrame(frame); };
  setInterval(() => { if (!document.hidden) poll(); }, 300);
  poll();
  requestAnimationFrame(frame);
})();
