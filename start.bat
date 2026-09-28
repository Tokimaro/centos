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

rem Ищем Python: сначала лаунчер py (python в Windows часто ведёт в Microsoft Store).
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (python -c "import sys" >nul 2>&1 && set "PY=python")
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto :oldpython

if not exist data\items.json (
  echo Скачиваю названия предметов...
  %PY% -m albion_trader update-items
)
start "" http://127.0.0.1:8484
%PY% -m albion_trader serve %*
pause
exit /b

:nopython
echo.
echo Python не найден.
echo Установите Python 3.10+ с https://www.python.org/downloads/
echo и отметьте в установщике "Add python.exe to PATH" и "py launcher".
echo Если Python установлен только для другой учётной записи, установите его
echo "for all users" - этот скрипт работает с правами администратора.
pause
exit /b 1

:oldpython
echo.
echo Нужен Python 3.10 или новее. Установленная версия:
%PY% --version
echo Скачайте новую версию: https://www.python.org/downloads/
pause
exit /b 1
