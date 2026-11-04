"""Поиск роликов по названию: по одной площадке на выбор или по всем сразу, ответы идут вперемешку.

Если одна площадка не ответила, остальные всё равно покажут свои результаты: упавший YouTube не
оставляет человека без поиска. Поиск есть не у всех площадок — у остальных работают ссылки
(см. `platforms.py`).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest

from app.config import MAX_SOURCE_SECONDS
from app.services import dailymotion, rutube, youtube
from app.services.downloader import DownloadError
from app.services.platforms import EVERYWHERE, SEARCHABLE, scope

log = logging.getLogger(__name__)

RESULTS = 8  # столько вариантов показываем человеку
MIN_QUERY_LENGTH = 2


class QueryTooShort(DownloadError):
    """Искать нечего: в запросе меньше двух символов. Отличается от прочих отказов тем, что повторять поиск незачем."""


def _usable(card: dict) -> bool:
    """Годится в нарезку: не прямой эфир, длина известна и не больше предела. Выбор из списка проверку ссылки не проходит."""
    return not card["live"] and 0 < card["duration"] <= MAX_SOURCE_SECONDS


def _search_youtube(query: str, limit: int) -> list[dict]:
    return [card for card in youtube.search(f"ytsearch{limit}:{query}", limit) if _usable(card)]


def _search_rutube(query: str, limit: int) -> list[dict]:
    return [card for card in rutube.search(query) if _usable(card)][:limit]


def _search_dailymotion(query: str, limit: int) -> list[dict]:
    return [card for card in dailymotion.search(query) if _usable(card)][:limit]


SEARCHERS: dict[str, Callable[[str, int], list[dict]]] = {
    "youtube": _search_youtube,
    "rutube": _search_rutube,
    "dailymotion": _search_dailymotion,
}
_SEARCH_ERRORS = (youtube.YoutubeError, rutube.RutubeError, dailymotion.DailymotionError)
# Когда на одной площадке пусто, а причина может быть не в запросе: целые сериалы там нередко идут по подписке.
_NOTHING_HINTS = {
    "rutube": " Ролики RuTube по подписке (PREMIER и др.) бот не показывает: скачать их нельзя.",
}


def interleave(lists: list[list[dict]], limit: int) -> list[dict]:
    """Результаты площадок по очереди: первый с одной, первый с другой, второй с первой… Не больше `limit`."""
    mixed = [item for group in zip_longest(*lists) for item in group if item is not None]
    return mixed[:limit]


def search_videos(query: str, limit: int = RESULTS, *, platform: str = EVERYWHERE) -> list[dict]:
    """Ролики по названию: с одной площадки (`platform` — её id) или со всех площадок с поиском.

    Пустой список не бывает: ничего не нашлось или площадка не ответила — `DownloadError` с понятным текстом.
    """
    text = " ".join((query or "").split())
    if len(text) < MIN_QUERY_LENGTH:
        raise QueryTooShort("Слишком короткое название — напишите хотя бы пару слов.")
    chosen = scope(platform, SEARCHABLE)
    if chosen is None:
        raise DownloadError("Такой площадки в поиске нет — выберите одну из кнопок или пришлите ссылку на ролик.")
    only = chosen[0] if len(chosen) == 1 else None

    def ask(name: str) -> list[dict] | None:
        try:
            return SEARCHERS[name](text, limit)
        except _SEARCH_ERRORS as error:
            log.warning("Поиск на %s не удался: %s", name, error)
            return None

    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        answers = list(pool.map(ask, [item.id for item in chosen]))
    if all(answer is None for answer in answers):
        if only is not None:
            raise DownloadError(f"{only.title} сейчас не отвечает — выберите другую площадку, попробуйте через пару минут или пришлите ссылку.")
        raise DownloadError("Поиск сейчас не отвечает — попробуйте через пару минут или пришлите ссылку.")

    found = interleave([answer for answer in answers if answer], limit)
    for card in found:
        card["title"] = card["title"] or "Без названия"
    if not found:
        if only is not None:
            raise DownloadError(
                f"На {only.title} по запросу «{text}» ничего не нашлось — попробуйте переформулировать, "
                f"выберите другую площадку или пришлите ссылку.{_NOTHING_HINTS.get(only.id, '')}"
            )
        raise DownloadError(f"По запросу «{text}» ничего не нашлось — попробуйте переформулировать или пришлите ссылку.")
    return found
