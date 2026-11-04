"""RuTube: превращение записей API в карточки и поиск. Сеть подменяется."""
import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from app.services import rutube
from app.services.rutube import RutubeError, search, video_card

VIDEO_ID = "0123456789abcdef0123456789abcdef"


def item(**overrides):
    return {
        "id": VIDEO_ID,
        "title": "  Подкаст про деньги ",
        "duration": 1800,
        "hits": 12_345,
        "publication_ts": "2026-06-29T21:20:14",
        "author": {"name": "Канал"},
        "thumbnail_url": "https://pic.rutubelist.ru/video/x.jpg",
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
        body: object = {"results": []}
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

    monkeypatch.setattr(rutube.urllib.request, "urlopen", fake_urlopen)
    return state


# ---------- карточка ----------


def test_item_becomes_a_card_in_the_common_shape():
    published = datetime(2026, 6, 29, 21, 20, 14, tzinfo=timezone.utc).timestamp()
    assert video_card(item()) == {
        "platform": "rutube",
        "video_id": VIDEO_ID,
        "title": "Подкаст про деньги",
        "duration": 1800.0,
        "uploader": "Канал",
        "views": 12_345,
        "live": False,
        "published": published,
        "url": f"https://rutube.ru/video/{VIDEO_ID}/",
        "thumbnail": "https://pic.rutubelist.ru/video/x.jpg",
    }


@pytest.mark.parametrize("flag", ["is_livestream", "is_adult", "is_hidden", "is_deleted", "is_audio", "is_paid", "is_locked"])
def test_streams_adult_hidden_deleted_audio_and_paid_are_dropped(flag):
    assert video_card(item(**{flag: True})) is None


def test_videos_behind_a_subscription_are_dropped():
    """PREMIER и другие: `is_paid` у них false, но плеер отвечает «по подписке» — скачать такой ролик нельзя."""
    assert video_card(item(common_subscription_product_codes=["PREMIER_RUTUBE_YAPPY", "PREMIER_RUTUBE_START"])) is None


@pytest.mark.parametrize("codes", [[], None])
def test_an_empty_subscription_list_is_an_ordinary_free_video(codes):
    assert video_card(item(common_subscription_product_codes=codes)) is not None


@pytest.mark.parametrize("broken", [{"id": ""}, {"title": ""}, {"title": "   "}, {"duration": 0}, {"duration": None}])
def test_items_without_id_title_or_duration_are_dropped(broken):
    assert video_card(item(**broken)) is None


def test_missing_optional_fields_do_not_break_the_card():
    card = video_card({"id": VIDEO_ID, "title": "Название", "duration": 600})
    assert card["uploader"] == "" and card["views"] == 0 and card["thumbnail"] == "" and card["published"] is None


def test_creation_date_is_used_when_there_is_no_publication_date():
    card = video_card(item(publication_ts=None, created_ts="2026-01-02T03:04:05"))
    assert card["published"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()


def test_a_garbage_date_means_unknown_not_a_crash():
    assert video_card(item(publication_ts="вчера"))["published"] is None


# ---------- поиск ----------


def test_search_asks_the_open_api_and_returns_cards(api):
    api.body = {"results": [item(), item(id="f" * 32, title="Второй ролик")]}

    cards = search("подкаст про деньги")

    assert [card["video_id"] for card in cards] == [VIDEO_ID, "f" * 32]
    [(request, timeout)] = api.requests
    assert request.full_url.startswith("https://rutube.ru/api/search/video/?")
    assert "query=%D0%BF%D0%BE%D0%B4%D0%BA%D0%B0%D1%81%D1%82" in request.full_url and "page=1" in request.full_url
    assert request.get_header("User-agent") == rutube.USER_AGENT
    assert timeout == rutube.TIMEOUT


def test_one_strange_item_does_not_spoil_the_list(api):
    api.body = {"results": [item(), "строка", None, item(id="e" * 32, duration="много"), item(id="d" * 32, is_adult=True), item(id="c" * 32)]}

    assert [card["video_id"] for card in search("x y")] == [VIDEO_ID, "c" * 32]


def test_search_never_offers_subscription_videos(api):
    premier = item(id="a" * 32, title="Интерны, 4 сезон, 98 серия", common_subscription_product_codes=["PREMIER_RUTUBE_START"])
    api.body = {"results": [premier, item()]}

    assert [card["video_id"] for card in search("интерны")] == [VIDEO_ID]


@pytest.mark.parametrize("body", [{}, {"results": None}, {"results": "нет"}, [], "ошибка", None])
def test_an_unexpected_shape_is_just_an_empty_result(api, body):
    api.body = body
    assert search("x y") == []


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("нет сети"),
        urllib.error.HTTPError("https://rutube.ru", 503, "unavailable", {}, None),
        TimeoutError("долго"),
        ConnectionResetError(),
    ],
)
def test_network_failures_become_a_russian_error(api, error):
    api.error = error
    with pytest.raises(RutubeError, match="RuTube сейчас не отвечает"):
        search("x y")


def test_broken_json_becomes_a_russian_error(api):
    api.raw = "<html>Ой</html>".encode()
    with pytest.raises(RutubeError, match="RuTube сейчас не отвечает"):
        search("x y")


# ---------- закрыт ли ролик подпиской ----------


def player_answer(**detail):
    """Так плеер RuTube отвечает про закрытый или несуществующий ролик (HTTP 244, единственный ключ `detail`)."""
    return {"detail": {"id": 2, "url": "", **detail}}


def test_paywall_names_the_subscription(api):
    api.body = player_answer(name="subscription: Подписка PREMIER", type="common_subscription")

    assert rutube.paywall(VIDEO_ID) == "PREMIER"
    [(request, timeout)] = api.requests
    assert request.full_url.startswith(f"https://rutube.ru/api/play/options/{VIDEO_ID}/?")
    assert request.get_header("User-agent") == rutube.USER_AGENT and timeout == rutube.TIMEOUT


def test_paywall_without_a_name_is_an_empty_string_not_none(api):
    api.body = player_answer(type="common_subscription")
    assert rutube.paywall(VIDEO_ID) == ""  # подписка есть, названия нет — вызывающему важно отличить это от None


@pytest.mark.parametrize(
    "body",
    [
        player_answer(name="default_does_not_exists_video", type="action_reason"),  # ролика нет
        {"video_balancer": {"m3u8": "https://example.test/x.m3u8"}, "title": "Обычный ролик"},  # играет
        {"detail": "Not found."},
        {},
        [],
    ],
)
def test_other_answers_are_not_a_paywall(api, body):
    api.body = body
    assert rutube.paywall(VIDEO_ID) is None


@pytest.mark.parametrize("error", [urllib.error.URLError("нет сети"), TimeoutError("долго")])
def test_paywall_is_unknown_when_the_player_does_not_answer(api, error):
    api.error = error
    assert rutube.paywall(VIDEO_ID) is None  # не знаем — значит не утверждаем, что ролик платный


def test_paywall_is_unknown_for_broken_json(api):
    api.raw = b"<html>"
    assert rutube.paywall(VIDEO_ID) is None


@pytest.mark.parametrize("video_id", ["", "../etc/passwd", "short", "z" * 32, None])
def test_paywall_never_asks_the_player_about_a_strange_id(api, video_id):
    assert rutube.paywall(video_id) is None
    assert api.requests == []
