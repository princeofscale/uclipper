"""YouTube без входа в аккаунт: поиск через yt-dlp и приведение результатов к общему виду.

Общий вид карточки ролика (его же отдаёт RuTube, см. `rutube.py`) — обычный словарь, потому что он
лежит в состоянии диалога: `platform, video_id, title, duration, uploader, views, live, url, thumbnail`.
"""
from __future__ import annotations

import re
import shutil

import yt_dlp

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIVE_STATES = frozenset({"is_live", "is_upcoming"})
SEARCH_TIMEOUT = 15


class YoutubeError(RuntimeError):
    """YouTube не ответил. Текст — по-русски, его увидит пользователь."""


def js_runtimes() -> dict[str, dict]:
    """JS-рантаймы для yt-dlp. Сам он включает только deno, поэтому перечисляем все найденные на PATH."""
    found = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    return found or {"deno": {}}


def video_card(entry: dict) -> dict | None:
    """Запись «плоского» поиска yt-dlp → карточка. `None`, если это не ролик (канал, плейлист)."""
    video_id = entry.get("id") or ""
    if not _VIDEO_ID.match(video_id):
        return None
    return {
        "platform": "youtube",
        "video_id": video_id,
        "title": (entry.get("title") or "").strip(),
        "duration": float(entry.get("duration") or 0),
        "uploader": entry.get("channel") or entry.get("uploader") or "",
        "views": int(entry.get("view_count") or 0),
        "live": entry.get("live_status") in _LIVE_STATES,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        # Превью строим по id: так оно есть всегда, даже если плоский поиск не отдал миниатюры.
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def search(target: str, limit: int) -> list[dict]:
    """Карточки роликов по запросу `ytsearchN:название` или по адресу страницы поиска YouTube."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "playlistend": limit,
        "js_runtimes": js_runtimes(),
        "socket_timeout": SEARCH_TIMEOUT,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(target, download=False)
    except yt_dlp.utils.YoutubeDLError as error:
        raise YoutubeError("YouTube сейчас не отвечает — попробуйте через пару минут.") from error
    cards = (video_card(entry) for entry in (result or {}).get("entries") or [] if entry)
    return [card for card in cards if card is not None]
