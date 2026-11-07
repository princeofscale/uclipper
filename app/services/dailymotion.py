"""Dailymotion без входа в аккаунт: открытый API (тот самый, что стоит за сайтом), токен не нужен.

Русскоязычного там немного, поэтому для русских запросов просим только ролики с языком `ru`. Записи приводятся
к тому же виду, что у YouTube и RuTube (см. `youtube.py`, `rutube.py`).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://api.dailymotion.com/videos"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIMEOUT = 12
MAX_RESPONSE_BYTES = 5_000_000
FIELDS = "id,title,duration,views_total,owner.screenname,thumbnail_360_url,thumbnail_240_url,created_time,onair,private,explicit"
FETCH = 30  # сколько записей просим: часть отсеется по длине
MAX_LIMIT = 100  # больше API не отдаёт (на большее значение отвечает ошибкой 400)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class DailymotionError(RuntimeError):
    """Dailymotion не ответил. Текст — по-русски, его увидит пользователь."""


def video_card(item: dict) -> dict | None:
    """Запись API → карточка. `None` для прямых эфиров, приватного и «взрослого» и записей без длины."""
    video_id = str(item.get("id") or "")
    title = str(item.get("title") or "").strip()
    duration = float(item.get("duration") or 0)
    if not video_id or not title or duration <= 0 or any(item.get(flag) for flag in ("onair", "private", "explicit")):
        return None
    created = item.get("created_time")
    return {
        "platform": "dailymotion",
        "video_id": video_id,
        "title": title,
        "duration": duration,
        "uploader": str(item.get("owner.screenname") or ""),
        "views": int(item.get("views_total") or 0),
        "live": False,
        "published": float(created) if isinstance(created, (int, float)) else None,
        "url": f"https://www.dailymotion.com/video/{video_id}",
        "thumbnail": str(item.get("thumbnail_360_url") or item.get("thumbnail_240_url") or ""),
    }


def search(query: str, limit: int = FETCH, *, sort: str = "relevance") -> list[dict]:
    """Ролики по запросу. `sort="trending"` — то же, но в порядке «сейчас смотрят»: так площадка идёт в «В тренде»."""
    params = {"search": query, "fields": FIELDS, "limit": min(limit, MAX_LIMIT), "sort": sort}
    if _CYRILLIC.search(query):
        params["language"] = "ru"
    request = urllib.request.Request(
        f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 — адрес зашит выше
            payload = json.loads(response.read(MAX_RESPONSE_BYTES))
    except (OSError, ValueError) as error:  # сеть, таймаут, HTTP-ошибка (всё это OSError) и битый JSON (ValueError)
        raise DailymotionError("Dailymotion сейчас не отвечает — попробуйте через пару минут.") from error
    if isinstance(payload, dict) and payload.get("error"):
        raise DailymotionError("Dailymotion сейчас не отвечает — попробуйте через пару минут.")
    rows = payload.get("list") if isinstance(payload, dict) else None
    cards = []
    for item in rows if isinstance(rows, list) else []:
        try:
            card = video_card(item) if isinstance(item, dict) else None
        except (TypeError, ValueError):  # странная запись в ответе — пропускаем её, а не весь список
            card = None
        if card is not None:
            cards.append(card)
    return cards
