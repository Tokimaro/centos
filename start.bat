@echo off
chcp 65001 >nul
rem Запуск Albion Trader: приёмник данных + веб-интерфейс на http://127.0.0.1:8484
cd /d "%~dp0"
if not exist data\items.json (
  echo Скачиваю названия предметов...
  python -m albion_trader update-items
)
start "" http://127.0.0.1:8484
python -m albion_trader serve %*
pause
