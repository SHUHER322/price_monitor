@echo off
chcp 65001 >nul
rem Запуск бота двойным кликом. Окно не закрывай: пока оно открыто, бот работает.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Не найдено окружение .venv. Создай его командами:
    echo   py -V:3.14 -m venv .venv
    echo   .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo Не найден файл .env. Скопируй .env.example в .env и заполни его.
    pause
    exit /b 1
)

title Price Monitor
echo Бот запускается. Остановить: Ctrl + C или закрыть окно.
echo.
".venv\Scripts\python.exe" -m app.main
echo.
echo Бот остановлен.
pause
