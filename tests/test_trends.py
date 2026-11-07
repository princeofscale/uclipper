"""«В тренде»: отбор русскоязычных роликов, сбор подборок YouTube и RuTube, кеш и подписи. Сеть подменяется."""
from __future__ import annotations

import asyncio
import base64
import urllib.parse

import pytest

from app.config import MAX_SOURCE_SECONDS
from app.services import rutube, trends
from app.services.platforms import EVERYWHERE, TRENDING
from app.services.trends import (
    CATEGORIES,
    CATEGORY_BY_ID,
    FETCHERS,
    FRESH_DAYS,
    MIN_SECONDS,
    MIN_VIEWS,
    PERIOD_TEXT,
    TrendCache,
    TrendsError,
    fetch_category,
    fetch_rutube,
    fetch_youtube,
    freshness_rank,
    keep_original_russian,
    looks_russian,
    pick_videos,
    search_filter,
    views_text,
)

DAY = 86400.0
NOW = 1_800_000_000.0


def card(video_id="aaaaaaaaaaa", title="Большой подкаст про деньги", duration=1800, views=500_000, uploader="Канал", platform="youtube", **extra):
    """Карточка ролика в общем виде площадок (см. `youtube.video_card` и `rutube.video_card`)."""
    return {
        "platform": platform,
        "video_id": video_id,
        "title": title,
        "duration": duration,
        "uploader": uploader,
        "views": views,
        "live": False,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        **extra,
    }


def rutube_card(video_id, days_ago, views=10_000, title=None, **extra):
    return card(
        video_id, title or f"Подкаст выпуск {video_id}", views=views, uploader=f"Канал {video_id}", platform="rutube",
        published=NOW - days_ago * DAY, url=f"https://rutube.ru/video/{video_id}/", thumbnail="", **extra,
    )  # fmt: skip


# ---------- язык ----------


@pytest.mark.parametrize(
    "title",
    ["Интервью с Дудем", "ПОДКАСТ | Как заработать миллион", "Минecraft прохождение серия 5", "Обзор фильма «Дюна 2»"],
)
def test_russian_titles_are_recognised(title):
    assert looks_russian(title)


@pytest.mark.parametrize(
    "title",
    ["How to make money in 2026", "Podcast #5 — Elon Musk", "Да", "", "123 456", "Қазақстан туралы бәрі", "Інтерв'ю з Зеленським", "Беларусь ўсё"],
)
def test_non_russian_or_barely_russian_titles_are_rejected(title):
    assert not looks_russian(title)


# ---------- отбор ----------


def test_good_card_becomes_an_entry_with_everything_the_bot_needs():
    [video] = pick_videos([card()], 5)
    assert video == {
        "platform": "youtube",
        "video_id": "aaaaaaaaaaa",
        "title": "Большой подкаст про деньги",
        "duration": 1800.0,
        "uploader": "Канал",
        "views": 500_000,
        "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "thumbnail": "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg",
    }


def test_pick_filters_out_unsuitable_cards():
    cards = [
        card("a1", "English only title here"),
        card("a2", duration=MIN_SECONDS - 1),  # уже шорт: нарезать нечего
        card("a3", duration=MAX_SOURCE_SECONDS + 1),  # слишком тяжело
        card("a4", views=MIN_VIEWS["youtube"] - 1),
        card("a5", live=True),
        card("a7", title=""),
        card("a8", duration=None),
        card("edge", duration=MAX_SOURCE_SECONDS),
        card("good"),
    ]
    assert [video["video_id"] for video in pick_videos(cards, 10)] == ["edge", "good"]


def test_views_threshold_depends_on_the_platform():
    # у RuTube просмотры скромнее: то, что на YouTube «никто не смотрел», там уже заметно
    views = MIN_VIEWS["rutube"] + 1
    assert views < MIN_VIEWS["youtube"]
    assert pick_videos([card("y", views=views)], 5) == []
    assert [v["video_id"] for v in pick_videos([card("r", views=views, platform="rutube")], 5)] == ["r"]
    assert [v["video_id"] for v in pick_videos([card("x", views=1_001, platform="unknown")], 5)] == ["x"]  # общий порог


def test_at_most_two_videos_per_channel_and_order_is_kept():
    cards = [card(f"v{n}", f"Подкаст выпуск номер {n}", uploader="Один") for n in range(4)] + [card("other", uploader="Другой")]
    assert [video["video_id"] for video in pick_videos(cards, 10)] == ["v0", "v1", "other"]


def test_limit_and_already_taken_ids_are_respected():
    cards = [card(f"v{n}", f"Подкаст выпуск номер {n}", uploader=f"Канал {n}") for n in range(6)]
    assert len(pick_videos(cards, 3)) == 3
    assert [video["video_id"] for video in pick_videos(cards, 10, taken={"v0", "v1"})][:2] == ["v2", "v3"]


def test_unknown_channels_are_not_capped():
    cards = [card(f"v{n}", f"Подкаст выпуск номер {n}", uploader="") for n in range(4)]
    assert len(pick_videos(cards, 10)) == 4  # без имени канала ограничивать нечем


# ---------- оригинальное название ----------


def test_videos_whose_original_title_is_not_russian_are_dropped(monkeypatch):
    originals = {"ru": "Подкаст про деньги и успех", "en": "Podcast about money", "unknown": None}
    monkeypatch.setattr(trends, "_original_title", lambda video_id: originals[video_id])
    videos = [{"video_id": key, "title": "Переведённое название ролика"} for key in originals]

    kept = keep_original_russian(videos)

    assert [video["video_id"] for video in kept] == ["ru", "unknown"]  # не проверилось — оставляем
    assert kept[0]["title"] == "Подкаст про деньги и успех"  # берём настоящее название, а не перевод


def test_no_videos_means_no_lookups(monkeypatch):
    monkeypatch.setattr(trends, "_original_title", lambda video_id: pytest.fail("лишний запрос"))
    assert keep_original_russian([]) == []


# ---------- YouTube: сбор подборки ----------


def test_fetch_youtube_takes_the_week_first_and_tops_up_from_the_month(monkeypatch):
    periods: list[str] = []

    def fake_search(queries, period):
        periods.append(period)
        base = 0 if period == "week" else 100
        return [card(f"{period}{n}", f"Подкаст выпуск номер {n}", views=1_000_000 - n, uploader=f"К{base + n}") for n in range(2)], True

    monkeypatch.setattr(trends, "_search_all", fake_search)
    monkeypatch.setattr(trends, "keep_original_russian", lambda videos: videos)

    result = fetch_youtube(("подкаст",), limit=3)

    assert periods == ["week", "month"]
    assert [video["video_id"] for video in result] == ["week0", "week1", "month0"]


def test_fetch_youtube_stops_after_the_week_when_it_is_enough(monkeypatch):
    periods: list[str] = []

    def fake_search(queries, period):
        periods.append(period)
        return [card(f"v{n}", f"Подкаст выпуск номер {n}", uploader=f"К{n}") for n in range(5)], True

    monkeypatch.setattr(trends, "_search_all", fake_search)
    monkeypatch.setattr(trends, "keep_original_russian", lambda videos: videos)

    assert len(fetch_youtube(("подкаст",), limit=3)) == 3
    assert periods == ["week"]


def test_fetch_youtube_reports_when_youtube_never_answers(monkeypatch):
    monkeypatch.setattr(trends, "_search_all", lambda queries, period: ([], False))

    with pytest.raises(TrendsError, match="YouTube сейчас не отвечает"):
        fetch_youtube(("подкаст",))


def test_empty_but_answered_is_just_an_empty_list(monkeypatch):
    monkeypatch.setattr(trends, "_search_all", lambda queries, period: ([], True))

    assert fetch_youtube(("подкаст",)) == []


def test_youtube_queries_are_merged_deduplicated_and_sorted_by_views(monkeypatch):
    answers = {
        "один": [card("a", views=10), card("b", views=300)],
        "два": [card("b", views=300), card("c", views=2000)],
    }
    monkeypatch.setattr(trends, "_search_youtube", lambda query, period: answers[query])

    merged, answered = trends._search_all(("один", "два"), "week")  # noqa: SLF001

    assert [item["video_id"] for item in merged] == ["c", "b", "a"] and answered


def test_one_failed_youtube_query_does_not_spoil_the_rest(monkeypatch):
    def fake(query, period):
        if query == "упал":
            raise TrendsError("YouTube сейчас не отвечает")
        return [card("ok")]

    monkeypatch.setattr(trends, "_search_youtube", fake)

    merged, answered = trends._search_all(("упал", "живой"), "week")  # noqa: SLF001
    assert [item["video_id"] for item in merged] == ["ok"] and answered

    merged, answered = trends._search_all(("упал",), "week")  # noqa: SLF001
    assert merged == [] and not answered


# ---------- RuTube ----------


def test_freshness_rank_counts_week_month_quarter_then_stale():
    assert FRESH_DAYS == (7, 30, 90)
    expectations = [(0, 0), (7, 0), (8, 1), (30, 1), (31, 2), (90, 2), (91, 3), (400, 3)]
    for days_ago, rank in expectations:
        assert freshness_rank(NOW - days_ago * DAY, NOW) == rank, days_ago
    assert freshness_rank(None, NOW) == 3  # дата неизвестна — как «давно»


def test_fetch_rutube_puts_the_fresh_first_then_the_most_viewed(monkeypatch):
    cards = [
        rutube_card("old", 200, views=9_000_000),  # старше квартала: вообще не берём
        rutube_card("month", 20, views=900_000),
        rutube_card("weekLow", 3, views=5_000),
        rutube_card("weekTop", 5, views=70_000),
        rutube_card("quarter", 60, views=8_000_000),
        card("nodate", platform="rutube", views=9_999_999, published=None),
    ]
    monkeypatch.setattr(trends, "_search_rutube_all", lambda queries: (cards, True))

    result = fetch_rutube(("подкаст",), limit=10, now=NOW)

    assert [video["video_id"] for video in result] == ["weekTop", "weekLow", "month", "quarter"]
    assert {video["platform"] for video in result} == {"rutube"}


def test_fetch_rutube_applies_the_same_quality_filters(monkeypatch):
    cards = [
        rutube_card("short", 1, duration=MIN_SECONDS - 1),
        rutube_card("english", 1, title="English only title here"),
        rutube_card("quiet", 1, views=MIN_VIEWS["rutube"] - 1),
        rutube_card("fine", 1),
    ]
    monkeypatch.setattr(trends, "_search_rutube_all", lambda queries: (cards, True))

    assert [video["video_id"] for video in fetch_rutube(("подкаст",), now=NOW)] == ["fine"]


def test_fetch_rutube_respects_the_limit(monkeypatch):
    cards = [rutube_card(f"v{n}", 1, views=1_000_000 - n) for n in range(10)]
    monkeypatch.setattr(trends, "_search_rutube_all", lambda queries: (cards, True))

    assert len(fetch_rutube(("подкаст",), limit=4, now=NOW)) == 4


def test_fetch_rutube_reports_when_rutube_never_answers(monkeypatch):
    monkeypatch.setattr(trends, "_search_rutube_all", lambda queries: ([], False))

    with pytest.raises(TrendsError, match="RuTube сейчас не отвечает"):
        fetch_rutube(("подкаст",))


def test_rutube_queries_are_merged_without_repeats_and_survive_a_failed_query(monkeypatch):
    answers = {"один": [rutube_card("a", 1), rutube_card("b", 1)], "два": [rutube_card("b", 1), rutube_card("c", 1)]}

    def fake(query):
        if query == "упал":
            raise rutube.RutubeError("упал")
        return answers[query]

    monkeypatch.setattr(rutube, "search", fake)

    merged, answered = trends._search_rutube_all(("один", "упал", "два"))  # noqa: SLF001
    assert [item["video_id"] for item in merged] == ["a", "b", "c"] and answered

    merged, answered = trends._search_rutube_all(("упал",))  # noqa: SLF001
    assert merged == [] and not answered


# ---------- подборка по жанру ----------


def test_fetch_category_asks_the_chosen_platform_with_the_genre_queries(monkeypatch):
    seen = []
    monkeypatch.setitem(FETCHERS, "rutube", lambda queries, limit: seen.append(("rutube", queries, limit)) or ["r"])
    monkeypatch.setitem(FETCHERS, "youtube", lambda queries, limit: seen.append(("youtube", queries, limit)) or ["y"])
    category = CATEGORY_BY_ID["podcast"]

    assert fetch_category(category, "rutube", 5) == ["r"]
    assert fetch_category(category) == ["y"]  # по умолчанию YouTube, восемь роликов
    assert seen == [("rutube", category.queries, 5), ("youtube", category.queries, 8)]


def test_movies_drop_music_compilations_and_kids_shows(monkeypatch):
    """В жанре «Кино и сериалы» на RuTube попадались сборники музыки и мультфильмы для малышей."""
    titles = [
        "Музыка 2026: хиты и трендовые ремиксы | Топ-1 Сборник",
        "Кухня | Сезон 1 | Серия 1 - 5",
        "Буба - Все серии подряд. Мультфильмы для детей",
        "МАШИНКИ - Сериал для мальчиков - Все серии подряд",
        "Три дороги. Все серии подряд. Мелодрама",
        "Лучшие песни из советских фильмов",
    ]
    asked = []
    monkeypatch.setitem(
        FETCHERS, "rutube", lambda queries, limit: asked.append(limit) or [{"title": title, "video_id": title} for title in titles]
    )

    found = fetch_category(CATEGORY_BY_ID["movies"], "rutube", 3)

    assert [video["title"] for video in found] == ["Кухня | Сезон 1 | Серия 1 - 5", "Три дороги. Все серии подряд. Мелодрама"]
    assert asked == [3 + trends.EXCLUDE_SPARE]  # просим с запасом: часть выдачи отсеется по словам жанра


def test_the_exclusion_words_are_only_for_genres_that_set_them(monkeypatch):
    monkeypatch.setitem(FETCHERS, "youtube", lambda queries, limit: [{"title": "Музыка и деньги", "video_id": "a"}])
    assert [video["title"] for video in fetch_category(CATEGORY_BY_ID["money"], "youtube")] == ["Музыка и деньги"]
    assert [category.id for category in CATEGORIES if category.exclude] == ["movies"]


def test_exclusion_ignores_letter_case():
    movies = CATEGORY_BY_ID["movies"]
    assert movies.rejects("МУЗЫКА 2026") and movies.rejects("Лучшие ХИТЫ") and not movies.rejects("Чушпан: разбор сериала")
    assert not CATEGORY_BY_ID["podcast"].rejects("Музыка")


def test_every_platform_with_trends_has_a_fetcher_and_a_period_caption():
    for platform in TRENDING:
        assert platform.id in FETCHERS and platform.id in PERIOD_TEXT


# ---------- запросы жанра на разных площадках ----------


def test_a_genre_may_ask_different_words_on_a_platform():
    movies = CATEGORY_BY_ID["movies"]
    assert movies.queries_for("youtube") == movies.queries  # без своих слов — общие
    assert movies.queries_for("rutube") != movies.queries
    assert movies.queries_for("unknown") == movies.queries


def test_other_genres_use_the_same_words_everywhere():
    for category in CATEGORIES:
        if category.id != "movies":
            assert all(category.queries_for(platform.id) == category.queries for platform in TRENDING)


def test_rutube_gets_whole_films_and_series_while_youtube_gets_reviews(monkeypatch):
    """На RuTube «обзор фильма» находит хоккейные матчи и кулинарию, зато там лежат целые фильмы и сериалы."""
    seen = {}
    for platform in TRENDING:
        monkeypatch.setitem(FETCHERS, platform.id, lambda queries, limit, name=platform.id: seen.setdefault(name, queries) and [])
    movies = CATEGORY_BY_ID["movies"]

    fetch_category(movies, "youtube")
    fetch_category(movies, "rutube")

    assert any("обзор" in query or "разбор" in query for query in seen["youtube"])
    assert any("фильм" in query or "сериал" in query for query in seen["rutube"])
    assert not any("обзор" in query for query in seen["rutube"])


def test_the_movies_genre_points_to_where_whole_films_live():
    note = CATEGORY_BY_ID["movies"].note
    assert "VK" in note and "OK" in note and "ссылк" in note


# ---------- фильтр поиска ----------


def test_search_filter_is_a_valid_url_safe_protobuf():
    for period, code in (("day", 2), ("week", 3), ("month", 4)):
        value = search_filter(period)
        raw = base64.urlsafe_b64decode(urllib.parse.unquote(value))
        assert raw[:2] == bytes([0x08, 0x03])  # sort=3: по просмотрам
        assert raw[-4:] == bytes([0x08, code, 0x10, 0x01])  # период и тип «видео»
        assert "/" not in value and "+" not in value


# ---------- подписи ----------


@pytest.mark.parametrize(
    ("views", "text"),
    [(950, "950"), (1_000, "1 тыс."), (87_300, "87 тыс."), (999_999, "1000 тыс."), (1_000_000, "1,0 млн"), (1_340_000, "1,3 млн"), (25_000_000, "25 млн")],
)
def test_views_text(views, text):
    assert views_text(views) == text


def test_categories_are_well_formed():
    assert len({category.id for category in CATEGORIES}) == len(CATEGORIES) == len(CATEGORY_BY_ID)
    for category in CATEGORIES:
        assert category.emoji and category.title and category.hint and category.queries
        assert all(looks_russian(query) or query for query in category.queries)
        for platform in (*TRENDING, None):  # callback_data кнопки жанра не длиннее 64 байт
            where = platform.id if platform else EVERYWHERE
            assert len(f"trend:{where}:{category.id}".encode()) <= 64
            assert category.queries_for(where)


# ---------- кеш ----------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_cache_expires_after_the_ttl():
    clock = Clock()
    cache = TrendCache(ttl=60, clock=clock)
    cache.put("k", [1])
    assert cache.get("k") == [1]
    clock.now += 59
    assert cache.get("k") == [1]
    clock.now += 2
    assert cache.get("k") is None


def test_cache_forgets_the_oldest_when_full():
    clock = Clock()
    cache = TrendCache(ttl=100, clock=clock, size=2)
    cache.put("a", 1)
    clock.now += 1
    cache.put("b", 2)
    clock.now += 1
    cache.put("c", 3)
    assert cache.get("a") is None and cache.get("b") == 2 and cache.get("c") == 3
    cache.forget("b")
    assert cache.get("b") is None


async def test_cached_fetches_once_even_for_simultaneous_requests():
    cache = TrendCache()
    calls = []

    def slow():
        calls.append(1)
        import time

        time.sleep(0.05)
        return ["result"]

    results = await asyncio.gather(*(cache.cached("k", slow) for _ in range(5)))

    assert results == [["result"]] * 5 and len(calls) == 1


async def test_failure_is_remembered_briefly_and_then_retried():
    clock = Clock()
    cache = TrendCache(clock=clock)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise TrendsError("недоступно")
        return ["ok"]

    with pytest.raises(TrendsError):
        await cache.cached("k", flaky)
    with pytest.raises(TrendsError):
        await cache.cached("k", flaky)  # сразу после неудачи площадку не долбим
    assert len(attempts) == 1

    clock.now += trends.FAILURE_SECONDS + 1
    assert await cache.cached("k", flaky) == ["ok"]
    assert len(attempts) == 2


# ---------- Dailymotion ----------


def dailymotion_card(video_id, days_ago, views=10_000, title=None, **extra):
    return card(
        video_id, title or f"Подкаст выпуск {video_id}", views=views, uploader=f"Канал {video_id}",
        platform="dailymotion", published=NOW - days_ago * DAY,
        url=f"https://www.dailymotion.com/video/{video_id}", thumbnail="", **extra,
    )  # fmt: skip


def test_fetch_dailymotion_takes_the_fresh_first_then_the_most_viewed(monkeypatch):
    cards = [
        dailymotion_card("old", 200, views=9_000_000),  # старше квартала — не берём
        dailymotion_card("month", 20, views=900_000),
        dailymotion_card("weekLow", 3, views=5_000),
        dailymotion_card("weekTop", 5, views=70_000),
    ]
    monkeypatch.setattr(trends, "_search_dailymotion_all", lambda queries: (cards, True))

    result = trends.fetch_dailymotion(("подкаст",), limit=10, now=NOW)

    assert [video["video_id"] for video in result] == ["weekTop", "weekLow", "month"]
    assert {video["platform"] for video in result} == {"dailymotion"}


def test_fetch_dailymotion_says_so_when_the_platform_is_silent(monkeypatch):
    monkeypatch.setattr(trends, "_search_dailymotion_all", lambda queries: ([], False))

    with pytest.raises(TrendsError):
        trends.fetch_dailymotion(("подкаст",), now=NOW)


def test_dailymotion_search_asks_the_api_for_trending(monkeypatch):
    """Поиск идёт с сортировкой «сейчас смотрят», а не по релевантности, и только по русским роликам."""
    from app.services import dailymotion

    asked = {}

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return b'{"list": []}'

    def fake_urlopen(request, timeout=0):
        asked["url"] = request.full_url
        return Answer()

    monkeypatch.setattr(dailymotion.urllib.request, "urlopen", fake_urlopen)

    assert dailymotion.search("подкаст", sort="trending") == []
    assert "sort=trending" in asked["url"] and "language=ru" in asked["url"]


def test_dailymotion_is_offered_in_the_trends_section():
    assert "dailymotion" in [platform.id for platform in TRENDING]
    assert "dailymotion" in FETCHERS and PERIOD_TEXT["dailymotion"]
