"""RuTube без входа в аккаунт: открытый поиск по их API (тот самый, что стоит за сайтом).

Ответ — до 100 роликов в порядке релевантности, с числом просмотров и датой публикации; карточки
приводятся к тому же виду, что у YouTube (см. `youtube.py`). Сортировки по просмотрам у API нет, поэтому
«самое просматриваемое» считает вызывающий.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SEARCH_URL = "https://rutube.ru/api/search/video/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIMEOUT = 12
MAX_RESPONSE_BYTES = 5_000_000  # настоящий ответ — около 400 КБ
_EXCLUDED_FLAGS = ("is_livestream", "is_adult", "is_hidden", "is_deleted", "is_audio")


class RutubeError(RuntimeError):
    """RuTube не ответил. Текст — по-русски, его увидит пользователь."""


def _timestamp(value: object) -> float | None:
    """`2026-06-29T21:20:14` → секунды эпохи. Часовой пояс не важен: нужны сутки, а не минуты."""
    try:
        return datetime.fromisoformat(str(value)[:19]).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def video_card(item: dict) -> dict | None:
    """Запись API → карточка. `None` для прямых эфиров, «взрослого» и скрытого, аудио и записей без длины."""
    video_id = str(item.get("id") or "")
    title = str(item.get("title") or "").strip()
    duration = float(item.get("duration") or 0)
    if not video_id or not title or duration <= 0 or any(item.get(flag) for flag in _EXCLUDED_FLAGS):
        return None
    author = item.get("author") or {}
    return {
        "platform": "rutube",
        "video_id": video_id,
        "title": title,
        "duration": duration,
        "uploader": str(author.get("name") or ""),
        "views": int(item.get("hits") or 0),
        "live": False,
        "published": _timestamp(item.get("publication_ts") or item.get("created_ts")),
        "url": f"https://rutube.ru/video/{video_id}/",
        "thumbnail": str(item.get("thumbnail_url") or ""),
    }


def search(query: str) -> list[dict]:
    """Ролики по запросу в порядке релевантности (первая страница ответа)."""
    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'query': query, 'page': 1})}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 — адрес зашит выше
            payload = json.loads(response.read(MAX_RESPONSE_BYTES))
    except (OSError, ValueError) as error:  # сеть, таймаут, HTTP-ошибка (всё это OSError) и битый JSON (ValueError)
        raise RutubeError("RuTube сейчас не отвечает — попробуйте через пару минут.") from error
    results = payload.get("results") if isinstance(payload, dict) else None
    cards = []
    for item in results or []:
        try:
            card = video_card(item) if isinstance(item, dict) else None
        except (TypeError, ValueError):  # странная запись в ответе — пропускаем её, а не весь список
            card = None
        if card is not None:
            cards.append(card)
    return cards
