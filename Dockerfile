# ClipBanner Bot: Python + FFmpeg + Node (Node нужен yt-dlp, чтобы решать JS-челленджи YouTube).
# Debian 13 (trixie): в нём Node 20+, а yt-dlp-ejs не работает со старым Node.
FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости отдельным слоем: он пересобирается только при смене requirements.txt.
# Тестовые пакеты (pytest*) в образ бота не нужны.
COPY requirements.txt .
RUN grep -viE '^\s*pytest' requirements.txt > /tmp/runtime-requirements.txt \
    && pip install -r /tmp/runtime-requirements.txt

COPY app ./app
COPY assets ./assets

# Бот работает не от root. Рабочая папка (кеш, баннеры, настройки) подключается томом.
RUN useradd --system --uid 10001 --home-dir /app bot \
    && mkdir -p /app/workspace && chown -R bot:bot /app/workspace
USER bot

CMD ["python", "-m", "app.bot.app"]
