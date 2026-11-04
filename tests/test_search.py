"""Поиск по названию: YouTube и RuTube спрашиваются вместе, ответы идут вперемешку. Сеть подменяется."""
import pytest

from app.config import MAX_SOURCE_SECONDS
from app.services import rutube, search, youtube
from app.services.downloader import DownloadError
from app.services.search import interleave, search_videos


def card(video_id, platform="youtube", duration=600.0, live=False, title="Ролик"):
    return {
        "platform": platform,
        "video_id": video_id,
        "title": title,
        "duration": duration,
        "uploader": "Канал",
        "views": 1000,
        "live": live,
        "url": f"https://example.test/{video_id}",
        "thumbnail": "",
    }


@pytest.fixture
def searchers(monkeypatch):
    """Подменяет оба поиска; возвращает словарь «площадка → что вернуть или бросить» и журнал вызовов."""

    class Fake:
        answers: dict = {"youtube": [], "rutube": []}
        calls: list = []

    fake = Fake()
    fake.answers = {"youtube": [], "rutube": []}
    fake.calls = []

    def make(name):
        def run(query, limit):
            fake.calls.append((name, query, limit))
            answer = fake.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return run

    for name in ("youtube", "rutube"):
        monkeypatch.setitem(search.SEARCHERS, name, make(name))
    return fake


# ---------- смешивание ----------


def test_interleave_alternates_platforms_and_respects_the_limit():
    a = [card("a1"), card("a2"), card("a3")]
    b = [card("b1", "rutube"), card("b2", "rutube")]
    assert [c["video_id"] for c in interleave([a, b], 10)] == ["a1", "b1", "a2", "b2", "a3"]
    assert [c["video_id"] for c in interleave([a, b], 3)] == ["a1", "b1", "a2"]
    assert interleave([], 5) == [] and interleave([[], []], 5) == []


# ---------- общий поиск ----------


def test_results_of_both_platforms_come_mixed(searchers):
    searchers.answers = {"youtube": [card("y1"), card("y2")], "rutube": [card("r1", "rutube"), card("r2", "rutube")]}

    found = search_videos("интервью")

    assert [c["video_id"] for c in found] == ["y1", "r1", "y2", "r2"]
    assert {name for name, *_ in searchers.calls} == {"youtube", "rutube"}


def test_the_query_is_cleaned_and_limit_is_passed_on(searchers):
    searchers.answers = {"youtube": [card("y1")], "rutube": []}

    search_videos("  интервью   дурова \n", limit=5)

    assert {(query, limit) for _, query, limit in searchers.calls} == {("интервью дурова", 5)}


def test_one_platform_down_still_gives_results_from_the_other(searchers):
    searchers.answers = {"youtube": youtube.YoutubeError("упал"), "rutube": [card("r1", "rutube")]}
    assert [c["video_id"] for c in search_videos("интервью")] == ["r1"]

    searchers.answers = {"youtube": [card("y1")], "rutube": rutube.RutubeError("упал")}
    assert [c["video_id"] for c in search_videos("интервью")] == ["y1"]


def test_every_platform_down_is_reported_as_a_search_outage(searchers):
    searchers.answers = {"youtube": youtube.YoutubeError("упал"), "rutube": rutube.RutubeError("упал")}
    with pytest.raises(DownloadError, match="Поиск сейчас не отвечает"):
        search_videos("интервью")


def test_nothing_found_is_reported_with_the_query(searchers):
    with pytest.raises(DownloadError, match="«интервью» ничего не нашлось"):
        search_videos("интервью")


@pytest.mark.parametrize("query", ["", " ", "a", None])
def test_too_short_queries_never_reach_the_platforms(searchers, query):
    with pytest.raises(DownloadError, match="Слишком короткое"):
        search_videos(query)
    assert searchers.calls == []


def test_untitled_results_get_a_placeholder(searchers):
    searchers.answers = {"youtube": [card("y1", title="")], "rutube": []}
    assert search_videos("интервью")[0]["title"] == "Без названия"


# ---------- что годится в нарезку ----------


def test_youtube_search_drops_streams_unknown_lengths_and_marathons(monkeypatch):
    cards = [
        card("good"),
        card("live", live=True),
        card("nolen", duration=0),
        card("long", duration=MAX_SOURCE_SECONDS + 1),
        card("edge", duration=MAX_SOURCE_SECONDS),
    ]
    monkeypatch.setattr(youtube, "search", lambda target, limit: cards)

    found = search._search_youtube("запрос", 8)  # noqa: SLF001

    assert [c["video_id"] for c in found] == ["good", "edge"]


def test_youtube_search_asks_for_exactly_the_wanted_number(monkeypatch):
    seen = []
    monkeypatch.setattr(youtube, "search", lambda target, limit: seen.append((target, limit)) or [])

    search._search_youtube("запрос", 8)  # noqa: SLF001

    assert seen == [("ytsearch8:запрос", 8)]


def test_rutube_search_drops_marathons_and_cuts_to_the_limit(monkeypatch):
    cards = [card("r1", "rutube"), card("rlong", "rutube", duration=MAX_SOURCE_SECONDS + 1)] + [
        card(f"r{n}", "rutube") for n in range(2, 12)
    ]
    monkeypatch.setattr(rutube, "search", lambda query: cards)

    found = search._search_rutube("запрос", 4)  # noqa: SLF001

    assert [c["video_id"] for c in found] == ["r1", "r2", "r3", "r4"]
