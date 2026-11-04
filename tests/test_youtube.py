"""YouTube: карточки роликов и поиск через yt-dlp. Сеть подменяется."""
import pytest
import yt_dlp

from app.services import youtube
from app.services.youtube import YoutubeError, js_runtimes, search, video_card

VIDEO_ID = "dQw4w9WgXcQ"


def entry(**overrides):
    return {"id": VIDEO_ID, "title": "  Заголовок ", "duration": 212, "channel": "Канал", "view_count": 1234, **overrides}


# ---------- карточка ----------


def test_search_entry_becomes_a_card_in_the_common_shape():
    assert video_card(entry()) == {
        "platform": "youtube",
        "video_id": VIDEO_ID,
        "title": "Заголовок",
        "duration": 212.0,
        "uploader": "Канал",
        "views": 1234,
        "live": False,
        "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg",
    }


@pytest.mark.parametrize("bad_id", ["", None, "short", "x" * 12, "channel/UC123", "UCxxxxxxxxxxxxxxxxxxxxxx"])
def test_channels_and_playlists_are_not_videos(bad_id):
    assert video_card(entry(id=bad_id)) is None


@pytest.mark.parametrize(("status", "live"), [("is_live", True), ("is_upcoming", True), ("was_live", False), ("not_live", False), (None, False)])
def test_live_and_upcoming_are_marked(status, live):
    assert video_card(entry(live_status=status))["live"] is live


def test_missing_numbers_and_names_do_not_break_the_card():
    card = video_card({"id": VIDEO_ID})
    assert card["title"] == "" and card["duration"] == 0.0 and card["views"] == 0 and card["uploader"] == ""
    assert video_card(entry(channel=None, uploader="Автор"))["uploader"] == "Автор"


# ---------- рантаймы ----------


def test_every_javascript_runtime_on_path_is_enabled(monkeypatch):
    # yt-dlp сам включает только deno; без JS-рантайма YouTube отдаёт урезанный набор форматов
    monkeypatch.setattr(youtube.shutil, "which", lambda name: f"/bin/{name}" if name in {"node", "bun"} else None)
    assert js_runtimes() == {"node": {}, "bun": {}}


def test_without_any_runtime_the_default_is_kept(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda name: None)
    assert js_runtimes() == {"deno": {}}


# ---------- поиск ----------


@pytest.fixture
def ydl(monkeypatch):
    class FakeYDL:
        instances = []
        result = {"entries": []}
        error = None

        def __init__(self, opts):
            self.opts = opts
            FakeYDL.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, target, download=False):
            self.target = target
            if FakeYDL.error:
                raise FakeYDL.error
            return FakeYDL.result

    FakeYDL.instances = []
    FakeYDL.result = {"entries": []}
    FakeYDL.error = None
    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_search_returns_only_video_cards_and_uses_a_flat_listing(ydl):
    ydl.result = {"entries": [entry(), None, {"id": "UCchannelchannelchannelxx"}, entry(id="aaaaaaaaaaa")]}

    cards = search("ytsearch5:интервью", 5)

    assert [card["video_id"] for card in cards] == [VIDEO_ID, "aaaaaaaaaaa"]
    options = ydl.instances[0].opts
    assert options["extract_flat"] is True and options["playlistend"] == 5
    assert options["socket_timeout"] == youtube.SEARCH_TIMEOUT
    assert ydl.instances[0].target == "ytsearch5:интервью"


@pytest.mark.parametrize("result", [None, {}, {"entries": None}])
def test_an_empty_answer_is_an_empty_list(ydl, result):
    ydl.result = result
    assert search("ytsearch5:x", 5) == []


def test_a_failed_search_becomes_a_russian_error(ydl):
    ydl.error = yt_dlp.utils.DownloadError("ERROR: unable to download webpage")
    with pytest.raises(YoutubeError, match="YouTube сейчас не отвечает"):
        search("ytsearch5:x", 5)
