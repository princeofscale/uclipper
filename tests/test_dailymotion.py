"""Dailymotion: превращение записей открытого API в карточки и поиск. Сеть подменяется."""
import io
import json
import urllib.error
import urllib.parse

import pytest

from app.services import dailymotion
from app.services.dailymotion import DailymotionError, search, video_card

VIDEO_ID = "x2ytik6"


def item(**overrides):
    return {
        "id": VIDEO_ID,
        "title": "  Подкаст про деньги ",
        "duration": 1400,
        "views_total": 4321,
        "owner.screenname": "Канал",
        "thumbnail_360_url": "https://s2.dmcdn.net/v/abc/x360",
        "thumbnail_240_url": "https://s2.dmcdn.net/v/abc/x240",
        "created_time": 1_437_513_680,
        "onair": False,
        "private": False,
        "explicit": False,
        **overrides,
    }


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api(monkeypatch):
    """Подменяет `urlopen`: отдаёт заданное тело или бросает заданную ошибку, запоминает запросы."""

    class Api:
        body: object = {"list": []}
        raw: bytes | None = None
        error: Exception | None = None
        requests: list = []

    state = Api()
    state.requests = []

    def fake_urlopen(request, timeout=None):
        state.requests.append((request, timeout))
        if state.error is not None:
            raise state.error
        return FakeResponse(state.raw if state.raw is not None else json.dumps(state.body).encode())

    monkeypatch.setattr(dailymotion.urllib.request, "urlopen", fake_urlopen)
    return state


def asked(api) -> dict:
    """Параметры единственного запроса к API."""
    [(request, _)] = api.requests
    return {key: values[0] for key, values in urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query).items()}


# ---------- карточка ----------


def test_item_becomes_a_card_in_the_common_shape():
    assert video_card(item()) == {
        "platform": "dailymotion",
        "video_id": VIDEO_ID,
        "title": "Подкаст про деньги",
        "duration": 1400.0,
        "uploader": "Канал",
        "views": 4321,
        "live": False,
        "published": 1_437_513_680.0,
        "url": f"https://www.dailymotion.com/video/{VIDEO_ID}",
        "thumbnail": "https://s2.dmcdn.net/v/abc/x360",
    }


@pytest.mark.parametrize("flag", ["onair", "private", "explicit"])
def test_live_private_and_adult_videos_are_dropped(flag):
    assert video_card(item(**{flag: True})) is None


@pytest.mark.parametrize("broken", [{"id": ""}, {"title": ""}, {"title": "   "}, {"duration": 0}, {"duration": None}])
def test_items_without_id_title_or_duration_are_dropped(broken):
    assert video_card(item(**broken)) is None


def test_missing_optional_fields_do_not_break_the_card():
    card = video_card({"id": VIDEO_ID, "title": "Название", "duration": 600})
    assert card["uploader"] == "" and card["views"] == 0 and card["thumbnail"] == "" and card["published"] is None


def test_a_smaller_thumbnail_is_used_when_there_is_no_big_one():
    assert video_card(item(thumbnail_360_url=None))["thumbnail"] == "https://s2.dmcdn.net/v/abc/x240"


# ---------- поиск ----------


def test_search_asks_the_open_api_and_returns_cards(api):
    api.body = {"list": [item(), item(id="x9", title="Второй ролик")]}

    cards = search("подкаст про деньги")

    assert [card["video_id"] for card in cards] == [VIDEO_ID, "x9"]
    [(request, timeout)] = api.requests
    assert request.full_url.startswith("https://api.dailymotion.com/videos?")
    assert request.get_header("User-agent") == dailymotion.USER_AGENT
    assert timeout == dailymotion.TIMEOUT
    params = asked(api)
    assert params["search"] == "подкаст про деньги" and params["sort"] == "relevance"
    assert params["fields"] == dailymotion.FIELDS and params["limit"] == str(dailymotion.FETCH)


def test_a_russian_query_asks_for_russian_videos_only(api):
    search("интервью")
    assert asked(api)["language"] == "ru"


def test_a_latin_query_is_not_limited_to_russian(api):
    search("joe rogan podcast")
    assert "language" not in asked(api)


def test_the_limit_never_exceeds_what_the_api_allows(api):
    search("интервью", limit=500)
    assert asked(api)["limit"] == str(dailymotion.MAX_LIMIT)


def test_one_strange_item_does_not_spoil_the_list(api):
    api.body = {"list": [item(), "строка", None, item(id="e1", duration="много"), item(id="d1", private=True), item(id="c1")]}

    assert [card["video_id"] for card in search("x y")] == [VIDEO_ID, "c1"]


@pytest.mark.parametrize("body", [{}, {"list": None}, {"list": "нет"}, [], "ошибка", None])
def test_an_unexpected_shape_is_just_an_empty_result(api, body):
    api.body = body
    assert search("x y") == []


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("нет сети"),
        urllib.error.HTTPError("https://api.dailymotion.com", 503, "unavailable", {}, None),
        TimeoutError("долго"),
        ConnectionResetError(),
    ],
)
def test_network_failures_become_a_russian_error(api, error):
    api.error = error
    with pytest.raises(DailymotionError, match="Dailymotion сейчас не отвечает"):
        search("x y")


def test_broken_json_becomes_a_russian_error(api):
    api.raw = "<html>Ой</html>".encode()
    with pytest.raises(DailymotionError, match="Dailymotion сейчас не отвечает"):
        search("x y")


def test_an_error_object_from_the_api_becomes_a_russian_error(api):
    api.body = {"error": {"code": 400, "message": "Too high value", "type": "invalid_parameter"}}
    with pytest.raises(DailymotionError, match="Dailymotion сейчас не отвечает"):
        search("x y")
