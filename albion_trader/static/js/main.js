"use strict";
// Запуск: все разделы уже зарегистрированы своими файлами.
App.start().catch((e) => {
  document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="bad">Не удалось загрузить интерфейс: ${esc(e.message)}</p>`);
});
refreshConn();
setInterval(refreshConn, 15000);
