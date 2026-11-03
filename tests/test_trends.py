"""«В тренде»: отбор русскоязычных роликов, кеш и подписи. Сеть не нужна — YouTube подменяется."""
from __future__ import annotations

import asyncio
import base64
import urllib.parse

import pytest

from app.services import trends
from app.services.trends import (
    CATEGORIES,
    CATEGORY_BY_ID,
    MIN_SECONDS,
    MIN_VIEWS,
    TrendCache,
    TrendsError,
    fetch_videos,
    keep_original_russian,
    looks_russian,
    pick_videos,
    search_filter,
    views_text,
)


def entry(video_id="aaaaaaaaaaa", title="Большой подкаст про деньги", duration=1800, views=500_000, channel="Канал", **extra):
    return {"id": video_id, "title": title, "duration": duration, "view_count": views, "channel": channel, **extra}


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


def test_good_entry_becomes_a_card_with_everything_the_bot_needs():
    [video] = pick_videos([entry()], 5)
    assert video == {
        "video_id": "aaaaaaaaaaa",
        "title": "Большой подкаст про деньги",
        "duration": 1800.0,
        "uploader": "Канал",
        "views": 500_000,
        "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "thumbnail": "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg",
    }


def test_pick_filters_out_unsuitable_entries():
    entries = [
        entry("a1", "English only title here"),
        entry("a2", duration=MIN_SECONDS - 1),  # уже шорт: нарезать нечего
        entry("a3", duration=5 * 3600),  # слишком тяжело
        entry("a4", views=MIN_VIEWS - 1),
        entry("a5", live_status="is_live"),
        entry("a6", live_status="is_upcoming"),
        entry("a7", title=""),
        entry("a8", duration=None),
        entry("good"),
    ]
    assert [video["video_id"] for video in pick_videos(entries, 10)] == ["good"]


def test_at_most_two_videos_per_channel_and_order_is_kept():
    entries = [entry(f"v{n}", f"Подкаст выпуск номер {n}", channel="Один") for n in range(4)] + [entry("other", channel="Другой")]
    assert [video["video_id"] for video in pick_videos(entries, 10)] == ["v0", "v1", "other"]


def test_limit_and_already_taken_ids_are_respected():
    entries = [entry(f"v{n}", f"Подкаст выпуск номер {n}", channel=f"Канал {n}") for n in range(6)]
    assert len(pick_videos(entries, 3)) == 3
    assert [video["video_id"] for video in pick_videos(entries, 10, taken={"v0", "v1"})][:2] == ["v2", "v3"]


def test_channel_falls_back_to_uploader_and_unknown_channels_are_not_capped():
    entries = [entry(f"v{n}", f"Подкаст выпуск номер {n}", channel=None, uploader=None) for n in range(4)]
    assert len(pick_videos(entries, 10)) == 4  # без имени канала ограничивать нечем


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


# ---------- сбор подборки ----------


def test_fetch_videos_takes_the_week_first_and_tops_up_from_the_month(monkeypatch):
    periods: list[str] = []

    def fake_search(queries, period):
        periods.append(period)
        base = 0 if period == "week" else 100
        return [entry(f"{period}{n}", f"Подкаст выпуск номер {n}", views=1_000_000 - n, channel=f"К{base + n}") for n in range(2)], True

    monkeypatch.setattr(trends, "_search_all", fake_search)
    monkeypatch.setattr(trends, "keep_original_russian", lambda videos: videos)

    result = fetch_videos(("подкаст",), limit=3)

    assert periods == ["week", "month"]
    assert [video["video_id"] for video in result] == ["week0", "week1", "month0"]


def test_fetch_videos_stops_after_the_week_when_it_is_enough(monkeypatch):
    periods: list[str] = []

    def fake_search(queries, period):
        periods.append(period)
        return [entry(f"v{n}", f"Подкаст выпуск номер {n}", channel=f"К{n}") for n in range(5)], True

    monkeypatch.setattr(trends, "_search_all", fake_search)
    monkeypatch.setattr(trends, "keep_original_russian", lambda videos: videos)

    assert len(fetch_videos(("подкаст",), limit=3)) == 3
    assert periods == ["week"]


def test_fetch_videos_reports_when_youtube_never_answers(monkeypatch):
    monkeypatch.setattr(trends, "_search_all", lambda queries, period: ([], False))

    with pytest.raises(TrendsError, match="YouTube сейчас не отвечает"):
        fetch_videos(("подкаст",))


def test_empty_but_answered_is_just_an_empty_list(monkeypatch):
    monkeypatch.setattr(trends, "_search_all", lambda queries, period: ([], True))

    assert fetch_videos(("подкаст",)) == []


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
        assert len(f"trend:{category.id}".encode()) <= 64
        assert all(looks_russian(query) or query for query in category.queries)


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
        await cache.cached("k", flaky)  # сразу после неудачи YouTube не долбим
    assert len(attempts) == 1

    clock.now += trends.FAILURE_SECONDS + 1
    assert await cache.cached("k", flaky) == ["ok"]
    assert len(attempts) == 2
