# TgRader Clipper: Python + FFmpeg + Node (Node нужен yt-dlp, чтобы решать JS-челленджи YouTube).
# yt-dlp требует Node 22+, а в Debian 13 из пакетов только Node 20 — поэтому готовый Node 22 (LTS) берём
# из официального образа: один файл, на тех же Debian 13 и glibc.
FROM node:22-trixie-slim AS node

FROM python:3.13-slim-trixie
COPY --from=node /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
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
