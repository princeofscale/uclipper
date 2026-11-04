"""«В тренде»: что сейчас смотрят русскоязычные зрители — сырьё для нарезки под TikTok и Shorts.

Официальных лент трендов у площадок нет (YouTube свою закрыл, у RuTube «Популярное» — подборка кино),
поэтому берём то, что доступно без входа в аккаунт, по запросам тех жанров, из которых получаются хорошие клипы:

• YouTube — поиск с фильтром «за неделю» и сортировкой по просмотрам (через yt-dlp);
• RuTube — открытый поиск их API; сортировки по просмотрам там нет, так что свежие ролики
  (неделя → месяц → квартал) упорядочиваем по просмотрам сами.

Две тонкости, которые пришлось учесть:

• поиск YouTube по-русски возвращает и английские ролики с машинным переводом названия — настоящее название
  узнаём через oEmbed (быстро, пачкой) и оставляем только то, что по-русски на самом деле;
• ролик короче пяти минут — уже готовый шорт, нарезать нечего, а слишком длинный бот не потянет.

Ответы кешируются на полчаса: данные меняются медленно, а площадки не любят одинаковые запросы по кругу.
Все функции блокирующие — зовите их через `TrendCache.cached` (он уводит работу в поток).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

from app.config import MAX_SOURCE_SECONDS
from app.services import rutube, youtube

T = TypeVar("T")

CACHE_SECONDS = 30 * 60
FAILURE_SECONDS = 3 * 60  # после неудачи не долбим источник чаще, чем раз в пару минут
MIN_SECONDS = 5 * 60  # короче — это уже готовый шорт, нарезать нечего
MAX_SECONDS = MAX_SOURCE_SECONDS  # длиннее бот всё равно не примет
MIN_VIEWS = {"youtube": 2_000, "rutube": 500}  # совсем непросматриваемое в «тренд» не пускаем (у RuTube числа скромнее)
DEFAULT_MIN_VIEWS = 1_000
PER_CHANNEL = 2  # столько роликов с одного канала в одной подборке, чтобы список не был однообразным
FETCH_LIMIT = 40  # сколько результатов просим у YouTube на один запрос
FRESH_DAYS = (7, 30, 90)  # RuTube: что считать свежим — сначала неделя, потом месяц, потом квартал
OEMBED_URL = "https://www.youtube.com/oembed?format=json&url="
USER_AGENT = rutube.USER_AGENT

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_NOT_RUSSIAN = re.compile(r"[әғқңөұүһіїєґўӘҒҚҢӨҰҮҺІЇЄҐЎ]")  # кириллица, но казахский, украинский, белорусский


class TrendsError(RuntimeError):
    """Не получилось узнать, что в тренде. Текст — по-русски, его увидит пользователь."""


@dataclass(frozen=True)
class Category:
    id: str  # короткий — он едет в callback_data
    emoji: str
    title: str
    queries: tuple[str, ...]  # что искать; короткие русские слова дают самую русскую выдачу
    hint: str  # чем хороши для нарезки
    note: str = ""  # пояснение под списком роликов (если жанр на площадках ведёт себя по-разному)
    overrides: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (площадка, свои слова) — где общие не подходят
    exclude: tuple[str, ...] = ()  # слова в названии, по которым ролик не из этого жанра (строчными буквами)

    def queries_for(self, platform: str) -> tuple[str, ...]:
        """Слова поиска для площадки: свои, если для неё заданы, иначе общие."""
        return dict(self.overrides).get(platform, self.queries)

    def rejects(self, title: str) -> bool:
        """Название выдаёт чужой жанр (например, сборник музыки среди сериалов)."""
        lowered = title.lower()
        return any(word in lowered for word in self.exclude)


CATEGORIES: tuple[Category, ...] = (
    Category("podcast", "🎙", "Подкасты и интервью", ("интервью", "подкаст"), "цитаты и острые моменты разговора"),
    Category("humor", "😂", "Юмор и стендап", ("стендап", "юмор"), "панчи, которые просят повторить"),
    Category("games", "🎮", "Игры и стримы", ("прохождение", "майнкрафт"), "хайлайты и неожиданные повороты"),
    Category("money", "💼", "Бизнес и деньги", ("деньги", "заработок"), "советы и разборы в паре фраз"),
    # На YouTube хороши разборы и мнения, а на RuTube «обзор фильма» находит матчи КХЛ и кулинарию — зато там лежат
    # целые российские фильмы и сериалы (платные ролики по подписке отсеивает `rutube.video_card`).
    Category(
        "movies", "🎬", "Кино и сериалы", ("обзор фильма", "обзор сериала", "разбор фильма"),
        "разборы и мнения, а на RuTube — целые фильмы и сериалы",
        note="Больше всего целых фильмов и сериалов в VK Видео и OK. Искать там бот не умеет: найдите серию сами и пришлите ссылку.",
        overrides=(("rutube", ("русский фильм", "российский сериал", "сериал все серии")),),
        # По этим запросам RuTube подмешивает сборники музыки и мультфильмы для малышей.
        exclude=("музыка", "хиты", "ремикс", "песни", "для детей", "для малышей", "для мальчиков", "для девочек"),
    ),  # fmt: skip
    Category("sport", "⚽", "Спорт", ("футбол", "хоккей", "бокс"), "голы, нокауты и громкие заявления"),
)
CATEGORY_BY_ID = {category.id: category for category in CATEGORIES}


# ---------------------------------------------------------------------------
# Кеш
# ---------------------------------------------------------------------------


class TrendCache:
    """Кеш с временем жизни. `cached` не даёт нескольким нажавшим одновременно гонять один запрос дважды."""

    def __init__(self, ttl: float = CACHE_SECONDS, *, clock: Callable[[], float] = time.monotonic, size: int = 64) -> None:
        self._ttl = ttl
        self._clock = clock
        self._size = size
        self._items: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str):
        found = self._items.get(key)
        if found is None:
            return None
        expires, value = found
        if self._clock() >= expires:
            del self._items[key]
            return None
        return value

    def put(self, key: str, value: object, ttl: float | None = None) -> None:
        if len(self._items) >= self._size:
            self._items.pop(min(self._items, key=lambda name: self._items[name][0]), None)
        self._items[key] = (self._clock() + (self._ttl if ttl is None else ttl), value)

    def forget(self, key: str) -> None:
        self._items.pop(key, None)

    def _hit(self, key: str):
        hit = self.get(key)
        if isinstance(hit, TrendsError):  # недавняя неудача тоже кешируется — чтобы не долбить источник
            raise hit
        return hit

    async def cached(self, key: str, fetch: Callable[[], T]) -> T:
        """Значение из кеша или результат `fetch()`, выполненного в потоке."""
        hit = self._hit(key)
        if hit is not None:
            return hit  # type: ignore[return-value]
        async with self._locks.setdefault(key, asyncio.Lock()):
            hit = self._hit(key)
            if hit is not None:
                return hit  # type: ignore[return-value]
            try:
                value = await asyncio.to_thread(fetch)
            except TrendsError as error:
                self.put(key, error, FAILURE_SECONDS)
                raise
            self.put(key, value)
            return value


# ---------------------------------------------------------------------------
# Отбор
# ---------------------------------------------------------------------------

_PERIODS = {"day": 2, "week": 3, "month": 4}  # значения фильтра «Дата загрузки» в поиске YouTube


def search_filter(period: str = "week") -> str:
    """Параметр `sp` поиска YouTube: тип «видео», период, сортировка по числу просмотров.

    Это protobuf, собранный руками: sort=3 (по просмотрам), в фильтрах — дата и тип (1 = видео).
    """
    filters = bytes([0x08, _PERIODS[period], 0x10, 0x01])
    raw = bytes([0x08, 0x03, 0x12, len(filters)]) + filters
    return urllib.parse.quote(base64.urlsafe_b64encode(raw).decode(), safe="")


def looks_russian(title: str) -> bool:
    """Название написано по-русски: не английское и не на казахском/украинском (там своя кириллица)."""
    if _NOT_RUSSIAN.search(title):
        return False
    letters = [char for char in title if char.isalpha()]
    count = len(_CYRILLIC.findall(title))
    return count >= 4 and count / len(letters) >= 0.4


def pick_videos(cards: list[dict], limit: int, *, taken: set[str] | None = None) -> list[dict]:
    """Оставляет русскоязычные ролики подходящей длины, не больше двух с канала. Порядок сохраняется.

    На входе карточки площадок (`youtube.video_card`, `rutube.video_card`), на выходе — то, что нужно боту.
    """
    seen = set(taken or ())
    per_channel: dict[str, int] = {}
    chosen: list[dict] = []
    for card in cards:
        video_id = card.get("video_id") or ""
        title = (card.get("title") or "").strip()
        duration = float(card.get("duration") or 0)
        views = int(card.get("views") or 0)
        if video_id in seen or not title or not looks_russian(title):
            continue
        if views < MIN_VIEWS.get(card.get("platform") or "", DEFAULT_MIN_VIEWS) or card.get("live"):
            continue
        if not MIN_SECONDS <= duration <= MAX_SECONDS:
            continue
        channel = card.get("uploader") or ""
        if channel and per_channel.get(channel, 0) >= PER_CHANNEL:
            continue
        seen.add(video_id)
        per_channel[channel] = per_channel.get(channel, 0) + 1
        chosen.append(
            {
                "platform": card["platform"],
                "video_id": video_id,
                "title": title,
                "duration": duration,
                "uploader": channel,
                "views": views,
                "url": card["url"],
                "thumbnail": card.get("thumbnail") or "",
            }
        )
        if len(chosen) >= limit:
            break
    return chosen


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def _search_youtube(query: str, period: str) -> list[dict]:
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&sp={search_filter(period)}"
    try:
        return youtube.search(url, FETCH_LIMIT)
    except youtube.YoutubeError as error:
        raise TrendsError(str(error)) from error


def _search_all(queries: tuple[str, ...], period: str) -> tuple[list[dict], bool]:
    """Результаты всех запросов разом, самые просматриваемые первыми. Второе значение: хоть один ответил."""

    def one(query: str) -> list[dict] | None:
        try:
            return _search_youtube(query, period)
        except TrendsError:
            return None

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        answers = list(pool.map(one, queries))
    merged: dict[str, dict] = {}
    for cards in answers:
        for card in cards or []:
            merged.setdefault(card["video_id"], card)
    ordered = sorted(merged.values(), key=lambda card: card["views"], reverse=True)
    return ordered, any(cards is not None for cards in answers)


def _original_title(video_id: str) -> str | None:
    """Настоящее название ролика (не машинный перевод) через oEmbed. `None` — узнать не вышло."""
    watch = urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    request = urllib.request.Request(OEMBED_URL + watch, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 — адрес зашит выше
            return str(json.loads(response.read(200_000)).get("title") or "") or None
    except (OSError, ValueError):
        return None


def keep_original_russian(videos: list[dict]) -> list[dict]:
    """Убирает английские ролики с переведённым названием. Если проверить не вышло — ролик остаётся."""
    if not videos:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(videos))) as pool:
        originals = list(pool.map(lambda video: _original_title(video["video_id"]), videos))
    kept = []
    for video, original in zip(videos, originals):
        if original is None:
            kept.append(video)
        elif looks_russian(original):
            kept.append({**video, "title": original})
    return kept


def fetch_youtube(queries: tuple[str, ...], limit: int = 8) -> list[dict]:
    """Самое просматриваемое по запросам: сначала за неделю, не хватило — добираем за месяц.

    Пустой список — ничего подходящего; `TrendsError` — YouTube не ответил.
    """
    chosen: list[dict] = []
    answered = False
    for period in ("week", "month"):
        cards, ok = _search_all(queries, period)
        answered = answered or ok
        if not ok:
            continue
        candidates = pick_videos(cards, limit * 3, taken={video["video_id"] for video in chosen})
        chosen += keep_original_russian(candidates)[: limit - len(chosen)]
        if len(chosen) >= limit:
            break
    if not answered:
        raise TrendsError("YouTube сейчас не отвечает — попробуйте через пару минут.")
    return chosen


# ---------------------------------------------------------------------------
# RuTube
# ---------------------------------------------------------------------------


def _search_rutube_all(queries: tuple[str, ...]) -> tuple[list[dict], bool]:
    """Ответы RuTube на все запросы, без повторов. Второе значение: хоть один ответил."""

    def one(query: str) -> list[dict] | None:
        try:
            return rutube.search(query)
        except rutube.RutubeError:
            return None

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        answers = list(pool.map(one, queries))
    merged: dict[str, dict] = {}
    for cards in answers:
        for card in cards or []:
            merged.setdefault(card["video_id"], card)
    return list(merged.values()), any(cards is not None for cards in answers)


def freshness_rank(published: float | None, now: float) -> int:
    """0 — выложено за неделю, 1 — за месяц, 2 — за квартал, 3 — давно (или дата неизвестна)."""
    if published is None:
        return len(FRESH_DAYS)
    age_days = (now - published) / 86400
    return next((rank for rank, days in enumerate(FRESH_DAYS) if age_days <= days), len(FRESH_DAYS))


def fetch_rutube(queries: tuple[str, ...], limit: int = 8, *, now: float | None = None) -> list[dict]:
    """Свежие ролики RuTube, самые просматриваемые первыми: сначала за неделю, не хватило — за месяц и квартал.

    Пустой список — ничего подходящего; `TrendsError` — RuTube не ответил.
    """
    cards, answered = _search_rutube_all(queries)
    if not answered:
        raise TrendsError("RuTube сейчас не отвечает — попробуйте через пару минут.")
    moment = time.time() if now is None else now
    fresh = [card for card in cards if freshness_rank(card.get("published"), moment) < len(FRESH_DAYS)]
    fresh.sort(key=lambda card: (freshness_rank(card.get("published"), moment), -card["views"]))
    return pick_videos(fresh, limit)


# ---------------------------------------------------------------------------
# Подборка по жанру
# ---------------------------------------------------------------------------

FETCHERS: dict[str, Callable[[tuple[str, ...], int], list[dict]]] = {"youtube": fetch_youtube, "rutube": fetch_rutube}
PERIOD_TEXT = {"youtube": "за неделю", "rutube": "за последнее время"}  # как называть период в подписях
EXCLUDE_SPARE = 4  # столько лишних роликов просим у площадки, если часть выдачи отсеется по словам жанра


def fetch_category(category: Category, platform: str = "youtube", limit: int = 8) -> list[dict]:
    if not category.exclude:
        return FETCHERS[platform](category.queries_for(platform), limit)
    videos = FETCHERS[platform](category.queries_for(platform), limit + EXCLUDE_SPARE)
    return [video for video in videos if not category.rejects(video["title"])][:limit]


# ---------------------------------------------------------------------------
# Подписи
# ---------------------------------------------------------------------------


def views_text(views: int) -> str:
    """`1_340_000` → «1,3 млн», `87_300` → «87 тыс.», `950` → «950»."""
    if views >= 10_000_000:
        return f"{views / 1_000_000:.0f} млн"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}".replace(".", ",") + " млн"
    if views >= 1_000:
        return f"{views / 1_000:.0f} тыс."
    return str(views)
