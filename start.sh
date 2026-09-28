#!/bin/sh
# Запуск Albion Trader: встроенный сборщик данных + веб-интерфейс на http://127.0.0.1:8484
# Для захвата пакетов нужен root (или: sudo setcap cap_net_raw+ep "$(readlink -f "$(command -v python3)")").
cd "$(dirname "$0")"
[ -f data/items.json ] || python3 -m albion_trader update-items
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  exec sudo python3 -m albion_trader serve "$@"
fi
exec python3 -m albion_trader serve "$@"
