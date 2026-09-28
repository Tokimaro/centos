@echo off
chcp 65001 >nul
rem Запускает albiondata-client так, чтобы данные уходили ТОЛЬКО в локальный Albion Trader.
rem Флаг -i заменяет адрес публичного сервера Albion Online Data Project.
rem Если в Albion Trader задан --token, добавьте его в адрес: http://127.0.0.1:8484/ВАШ_ТОКЕН
set CLIENT="C:\Program Files\Albion Data Client\albiondata-client.exe"
if not exist %CLIENT% (
  echo Не найден %CLIENT% - поправьте путь в этом файле.
  pause
  exit /b 1
)
%CLIENT% -i http://127.0.0.1:8484
