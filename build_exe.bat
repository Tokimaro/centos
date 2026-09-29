@echo off
chcp 65001 >nul
rem Сборка AlbionTrader.exe (нужен Python 3.10+ для Windows).
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller || goto :error
py -3 -m PyInstaller --noconfirm --onefile --noconsole --uac-admin --name AlbionTrader ^
  --icon albion_trader\static\icon.ico --add-data "albion_trader\static;albion_trader\static" launcher.py || goto :error
echo.
echo Готово: dist\AlbionTrader.exe
pause
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
