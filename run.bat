@echo off
rem ClipBanner Bot: установка и запуск Telegram-бота одной командой (Windows).
rem   run.bat          — обычный запуск (нужен .env с токеном)
rem   run.bat --check  — только проверить окружение
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where ffmpeg >nul 2>nul
if errorlevel 1 goto no_ffmpeg
where ffprobe >nul 2>nul
if errorlevel 1 goto no_ffmpeg

if exist ".venv\Scripts\python.exe" goto have_venv
echo Создаю виртуальное окружение .venv ...
py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo Не найден Python 3.11 или новее. Установите его с python.org и запустите run.bat снова.
  pause
  exit /b 1
)

:have_venv
echo Проверяю зависимости ...
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверьте интернет-соединение.
  pause
  exit /b 1
)

rem --- Токен: из переменной окружения или из .env ---
if not "%TELEGRAM_BOT_TOKEN%"=="" goto have_token
if exist ".env" (
  findstr /r /c:"^[ ]*TELEGRAM_BOT_TOKEN=." .env >nul 2>nul
  if not errorlevel 1 goto have_token
)
echo Не задан токен бота.
echo   1) Скопируйте шаблон:  copy .env.example .env
echo   2) Впишите токен от @BotFather в строку TELEGRAM_BOT_TOKEN=...
echo   3) Запустите run.bat снова
pause
exit /b 1

:have_token
if /i "%~1"=="--check" (
  echo Всё на месте: Python и FFmpeg найдены, токен задан.
  exit /b 0
)

echo ClipBanner Bot запускается. Остановить: Ctrl+C
".venv\Scripts\python.exe" -m app.bot.app
goto :eof

:no_ffmpeg
echo Не найден FFmpeg (нужны команды ffmpeg и ffprobe). Установите: winget install ffmpeg
echo После установки откройте новое окно терминала и запустите run.bat снова.
pause
exit /b 1
