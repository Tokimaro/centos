@echo off
chcp 65001 >nul
rem Самопроверка всех вкладок на ваших данных. Отчёт: data\check_report.txt
rem Лучше запускать, когда Albion Trader уже работает, — тогда проверится и сборщик.
cd /d "%~dp0"
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (python -c "import sys" >nul 2>&1 && set "PY=python")
if not defined PY (
  echo Python не найден. Установите Python 3.10+ с https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% -m albion_trader --data-dir data check %*
echo.
echo Отчёт сохранён в data\check_report.txt — пришлите его разработчику.
start "" notepad data\check_report.txt
pause
