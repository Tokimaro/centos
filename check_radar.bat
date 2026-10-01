@echo off
chcp 65001 >nul
rem Проверка радара на этом компьютере.
rem   1) автотесты (если программа из исходников);
rem   2) проверка окружения, сети, файлов и работающей программы (отчёт data\radar_check.txt);
rem   3) окно радара: «поверх игры» и оверлей — окно откроется само на несколько секунд;
rem   4) страница самотеста интерфейса в браузере.
rem Запускайте, когда Albion Trader уже работает (лучше — и игра запущена, вы в зоне).
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
  echo === Автотесты ===
  %PY% -m unittest discover -s tests
  if errorlevel 1 (echo. & echo [!] Есть упавшие автотесты — пришлите вывод выше разработчику.) else (echo [OK] Автотесты прошли.)
  echo.
)
echo === Проверка радара ===
%PY% -m albion_trader --data-dir data check-radar --window %*
echo.
echo Отчёт: data\radar_check.txt. В браузере открыта страница самотеста радара —
echo нажмите там «Скопировать результат», если нужно прислать его разработчику.
start "" notepad data\radar_check.txt
pause
