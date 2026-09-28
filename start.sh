#!/bin/sh
# Запуск Albion Trader: приёмник данных + веб-интерфейс на http://127.0.0.1:8484
cd "$(dirname "$0")"
[ -f data/items.json ] || python3 -m albion_trader update-items
exec python3 -m albion_trader serve "$@"
