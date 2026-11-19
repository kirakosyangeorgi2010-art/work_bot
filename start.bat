@echo off
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*' }) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo Бот уже запущен. Чтобы перезапустить, сначала запустите stop.bat
    pause
    exit /b
)

echo Запускаю бота в фоне...
powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c python -u bot.py > bot.log 2>&1' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

ping -n 6 127.0.0.1 >nul

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*' }) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo.
    echo Бот не запустился. Последние строки журнала:
    echo.
    powershell -NoProfile -Command "Get-Content bot.log -Tail 10 -Encoding UTF8"
    echo.
    pause
    exit /b
)

echo.
echo Бот работает в фоне. Это окно можно закрыть.
echo Остановить бота: stop.bat    Журнал: bot.log
ping -n 6 127.0.0.1 >nul
