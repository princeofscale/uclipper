#!/usr/bin/env bash
# ClipBanner Bot: установка и запуск Telegram-бота одной командой.
#   ./run.sh                    — обычный запуск
#   PYTHON=python3.12 ./run.sh  — выбрать конкретный Python (нужен 3.11 или новее)
#   ./run.sh --check            — только проверить окружение, без запуска бота
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# --- FFmpeg ---------------------------------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Не найден FFmpeg (нужны команды ffmpeg и ffprobe). Установите его и запустите ./run.sh снова:
  macOS:         brew install ffmpeg
  Ubuntu/Debian: sudo apt install ffmpeg
  Windows:       winget install ffmpeg   (или запустите run.bat)
EOF
  exit 1
fi

# --- Python 3.11+ ---------------------------------------------------------
pick_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/python ]; then
  if ! BASE_PYTHON="$(pick_python)"; then
    echo "Не найден Python 3.11 или новее. Установите его (macOS: brew install python) и запустите ./run.sh снова." >&2
    exit 1
  fi
  echo "Создаю виртуальное окружение .venv ($("$BASE_PYTHON" --version))…"
  "$BASE_PYTHON" -m venv .venv
fi

# --- Зависимости: ставим только когда requirements.txt изменился ------------
STAMP=".venv/.requirements.sha"
CURRENT="$(shasum requirements.txt | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$CURRENT" ]; then
  echo "Устанавливаю зависимости (первый запуск занимает минуту-две)…"
  .venv/bin/python -m pip install --quiet --upgrade pip || true
  .venv/bin/python -m pip install --quiet -r requirements.txt
  echo "$CURRENT" > "$STAMP"
fi

# --- Токен ----------------------------------------------------------------
have_token() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && return 0
  [ -f .env ] && grep -qE '^[[:space:]]*TELEGRAM_BOT_TOKEN=[^[:space:]]+' .env
}

if ! have_token; then
  cat >&2 <<'EOF'
Не задан токен бота.
  1) Скопируйте шаблон:  cp .env.example .env
  2) Впишите токен от @BotFather в строку TELEGRAM_BOT_TOKEN=...
  3) Запустите ./run.sh снова
EOF
  exit 1
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo "Всё на месте: Python $("$(command -v .venv/bin/python)" --version 2>/dev/null || .venv/bin/python --version), FFmpeg $(ffmpeg -version | head -1 | cut -d' ' -f3), токен найден."
  exit 0
fi

echo "ClipBanner Bot запускается. Остановить: Ctrl+C"
exec .venv/bin/python -m app.bot
