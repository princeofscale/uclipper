# TgRader Clipper: Python + FFmpeg + Node (Node нужен yt-dlp, чтобы решать JS-челленджи YouTube).
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
# Всё для тестов лежит в requirements-dev.txt и в образ бота не попадает.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY assets ./assets

# Бот работает не от root. Рабочая папка (кеш, баннеры, настройки) подключается томом.
RUN useradd --system --uid 10001 --home-dir /app bot \
    && mkdir -p /app/workspace && chown -R bot:bot /app/workspace
USER bot

CMD ["python", "-m", "app.bot"]
