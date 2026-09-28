@echo off
chcp 65001 >nul
rem Запуск Albion Trader: встроенный сборщик данных + веб-интерфейс на http://127.0.0.1:8484
rem Для чтения сетевого трафика игры нужны права администратора.
net session >nul 2>&1
if errorlevel 1 (
  echo Запрашиваю права администратора...
  if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  ) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  )
  exit /b
)
cd /d "%~dp0"
if not exist data\items.json (
  echo Скачиваю названия предметов...
  python -m albion_trader update-items
)
start "" http://127.0.0.1:8484
python -m albion_trader serve %*
pause
