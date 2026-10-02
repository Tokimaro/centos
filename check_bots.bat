@echo off
chcp 65001 >nul
rem Проверка ботов на этом компьютере.
rem   1) автотесты ботов (если программа из исходников);
rem   2) функции Windows, права, окна игры и их порты, работающая программа:
rem      персонажи, трафик, калибровка, макросы (отчёт data\bots_check.txt);
rem   3) переключение на окно игры и обратно.
rem Запускайте, когда Albion Trader работает, боты включены и окна игры открыты.
cd /d "%~dp0"
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (python -c "import sys" >nul 2>&1 && set "PY=python")
if not defined PY (
  echo Python не найден. Установите Python 3.10+ с https://www.python.org/downloads/
  pause
  exit /b 1
)
if exist tests (
  echo === Автотесты ботов ===
  %PY% -m unittest tests.test_bots tests.test_bot_nav tests.test_bot_win tests.test_bot_check
  if errorlevel 1 (echo. & echo [!] Есть упавшие автотесты — пришлите вывод выше разработчику.) else (echo [OK] Автотесты прошли.)
  echo.
)
echo === Проверка ботов ===
%PY% -m albion_trader --data-dir data check-bots --focus %*
echo.
echo Отчёт: data\bots_check.txt
start "" notepad data\bots_check.txt
pause
