@echo off
chcp 65001 >nul

powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo Бот сейчас не запущен.
) else (
    echo Бот остановлен.
)
ping -n 4 127.0.0.1 >nul
