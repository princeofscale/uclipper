"""Загрузчик: ссылки площадок, кеш по ключу ролика, опции yt-dlp, объяснение ошибок, локальные файлы.

yt-dlp подменяется фейком (в тестах нет сети); всё остальное — настоящий код: файлы, кеш, ремукс через ffmpeg.
"""
import io
import shutil
import subprocess
from pathlib import Path

import pytest
import yt_dlp

from app.config import MAX_SOURCE_SECONDS
from app.services import downloader, rutube, youtube
from app.services.capacity import DEFAULT_DOWNLOAD_THREADS
from app.services.downloader import (
    DownloadError,
    cached_source_path,
    download_video_cached,
    fetch_video_info,
    resolve_upload,
    save_local_upload,
)

VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
RUTUBE_ID = "0123456789abcdef0123456789abcdef"
RUTUBE_URL = f"https://rutube.ru/video/{RUTUBE_ID}/"
RUTUBE_KEY = f"rutube-{RUTUBE_ID}"
VK_URL = "https://vk.com/video-22822305_456239018"
FIRSTTV_URL = "https://www.1tv.ru/shows/pust-govoryat/vypuski-i-dramatichnye-momenty/vypusk-ot-03-11-2026"


def video_info(**overrides):
    return {
        "id": VIDEO_ID,
        "title": "Заголовок",
        "duration": 212,
        "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
        "uploader": "Автор",
        **overrides,
    }


@pytest.fixture
def ydl(monkeypatch):
    """Фейк yt_dlp.YoutubeDL: запоминает опции, отдаёт заданную инфу, «скачивает» файл в outtmpl."""

    class FakeYDL:
        instances = []
        info = {}
        error = None
        output_ext = "mp4"
        output_bytes = b"x" * (200 * 1024)
        progress_events = []
        downloaded = []

        def __init__(self, opts):
            self.opts = opts
            FakeYDL.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            self.asked = url
            if FakeYDL.error:
                raise FakeYDL.error
            return FakeYDL.info

        def download(self, urls):
            FakeYDL.downloaded.extend(urls)
            if FakeYDL.error:
                raise FakeYDL.error
            for event in FakeYDL.progress_events:
                for hook in self.opts.get("progress_hooks", []):
                    hook(event)
            Path(self.opts["outtmpl"].replace("%(ext)s", FakeYDL.output_ext)).write_bytes(FakeYDL.output_bytes)

    FakeYDL.instances, FakeYDL.downloaded = [], []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


# ---------- чужие ссылки ----------


def test_unknown_links_are_refused_before_any_network_call(ydl, tmp_path):
    with pytest.raises(DownloadError, match="YouTube.*RuTube.*VK Видео"):  # человеку сразу видно, что подходит
        fetch_video_info("https://example.com/x")
    with pytest.raises(DownloadError):
        download_video_cached("https://example.com/x", cache_dir=tmp_path)
    with pytest.raises(DownloadError):
        download_video_cached("https://youtube.com.evil.com/watch?v=" + VIDEO_ID, cache_dir=tmp_path)
    assert ydl.instances == []


# ---------- кеш и скачивание ----------


@pytest.mark.parametrize(("url", "key"), [(URL, VIDEO_ID), (RUTUBE_URL, RUTUBE_KEY)])
def test_cached_video_is_returned_without_touching_yt_dlp(ydl, tmp_path, url, key):
    cached = tmp_path / f"source_{key}.mp4"
    cached.write_bytes(b"c" * (200 * 1024))
    assert download_video_cached(url, cache_dir=tmp_path) == cached
    assert cached_source_path(key, tmp_path) == cached
    assert ydl.instances == []


def test_youtube_cache_keeps_its_old_file_name(ydl, tmp_path):
    # ролики, скачанные до появления других площадок, лежат как source_<id>.mp4 — они должны находиться и дальше
    old = tmp_path / f"source_{VIDEO_ID}.mp4"
    old.write_bytes(b"c" * (200 * 1024))
    assert download_video_cached(f"https://youtu.be/{VIDEO_ID}", cache_dir=tmp_path) == old


def test_tiny_cache_file_is_not_trusted(ydl, tmp_path):
    broken = tmp_path / f"source_{VIDEO_ID}.mp4"
    broken.write_bytes(b"c" * 10 * 1024)  # меньше 100 КБ — остаток оборванной загрузки
    assert cached_source_path(VIDEO_ID, tmp_path) is None
    result = download_video_cached(URL, cache_dir=tmp_path)
    assert result == broken
    assert result.stat().st_size == 200 * 1024
    assert len(ydl.instances) == 1


def test_download_passes_the_options_from_the_spec_to_yt_dlp(ydl, tmp_path):
    download_video_cached(URL, 720, cache_dir=tmp_path)
    opts = ydl.instances[0].opts
    assert opts["format"] == (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]"
        "/bestvideo[height<=?720]+bestaudio/best[height<=?720]/best"
    )  # хвост — запасные варианты для площадок без mp4+m4a или без указанной высоты
    assert opts["merge_output_format"] == "mp4"
    assert opts["noplaylist"] is True
    assert opts["playlistend"] == 1  # если по ссылке вдруг не один ролик, на диск не уйдёт вся передача целиком
    assert opts["outtmpl"] == str(tmp_path / f"source_{VIDEO_ID}.%(ext)s")
    # при одинаковом разрешении берём H.264, а не AV1: он декодируется везде и быстрее
    assert opts["format_sort"] == ["res", "vcodec:h264"]
    assert ydl.downloaded == [URL]


def test_download_takes_several_stream_pieces_at_once(ydl, tmp_path, monkeypatch):
    # HLS и DASH приходят нарезанными на куски: по одному их качать заметно дольше, чем держит канал
    monkeypatch.delenv("DOWNLOAD_THREADS", raising=False)
    download_video_cached(URL, cache_dir=tmp_path)
    assert ydl.instances[0].opts["concurrent_fragment_downloads"] == DEFAULT_DOWNLOAD_THREADS


def test_the_number_of_pieces_follows_the_environment(ydl, tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_THREADS", "1")
    download_video_cached(URL, cache_dir=tmp_path)
    assert ydl.instances[0].opts["concurrent_fragment_downloads"] == 1


def test_other_platforms_get_their_own_file_name_and_no_javascript_runtime(ydl, tmp_path):
    download_video_cached(RUTUBE_URL, 720, cache_dir=tmp_path)
    opts = ydl.instances[0].opts
    assert opts["outtmpl"] == str(tmp_path / f"source_{RUTUBE_KEY}.%(ext)s")
    assert opts["noplaylist"] is True and "js_runtimes" not in opts
    assert ydl.downloaded == [RUTUBE_URL]
    assert (tmp_path / f"source_{RUTUBE_KEY}.mp4").is_file()


def test_the_same_video_id_on_different_platforms_never_shares_a_cache_file(ydl, tmp_path):
    download_video_cached(VK_URL, cache_dir=tmp_path)
    download_video_cached(RUTUBE_URL, cache_dir=tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["source_vk--22822305_456239018.mp4", f"source_{RUTUBE_KEY}.mp4"]
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_a_non_mp4_result_ends_up_under_the_cache_name(ydl, tmp_path):
    mkv = tmp_path / "made.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-b:v", "2M", "-c:a", "aac",
         str(mkv)],
        check=True,
    )  # fmt: skip
    ydl.output_ext, ydl.output_bytes = "mkv", mkv.read_bytes()
    result = download_video_cached(URL, cache_dir=tmp_path / "cache")
    assert result == tmp_path / "cache" / f"source_{VIDEO_ID}.mp4"
    assert result.stat().st_size > 100 * 1024
    assert not (tmp_path / "cache" / f"source_{VIDEO_ID}.mkv").exists()


def test_an_empty_download_is_reported_instead_of_returning_a_stub(ydl, tmp_path):
    ydl.output_bytes = b"x" * 10
    with pytest.raises(DownloadError, match="файл с видео не появился"):
        download_video_cached(URL, cache_dir=tmp_path)


def test_download_progress_goes_from_zero_to_one_across_video_and_audio(ydl, tmp_path):
    ydl.progress_events = [
        {"status": "downloading", "filename": "v.f137.mp4", "downloaded_bytes": 50, "total_bytes": 100},
        {"status": "finished", "filename": "v.f137.mp4"},
        {"status": "downloading", "filename": "a.f140.m4a", "downloaded_bytes": 25, "total_bytes_estimate": 100},
        {"status": "finished", "filename": "a.f140.m4a"},
    ]
    seen = []
    download_video_cached(URL, cache_dir=tmp_path, on_progress=seen.append)
    assert seen == [0.25, 0.5, 0.625, 1.0]


def test_download_progress_reaches_one_when_the_file_is_a_single_stream(ydl, tmp_path):
    ydl.progress_events = [
        {"status": "downloading", "filename": "clip.mp4", "downloaded_bytes": 50, "total_bytes": 100},
        {"status": "finished", "filename": "clip.mp4"},
    ]
    seen = []
    download_video_cached(RUTUBE_URL, cache_dir=tmp_path, on_progress=seen.append)
    assert seen == [0.25, 0.5, 1.0]  # HLS и клипы приходят одним файлом: без финального шага шкала застряла бы на половине


# ---------- ошибки площадок ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "приватн"),
        ("ERROR: [youtube] x: Sign in to confirm you’re not a bot", "не бот"),
        ("ERROR: [youtube] x: Video unavailable", "недоступно"),
        ("ERROR: [rutube] x: Unable to download JSON metadata: HTTP Error 404: Not Found", "Такого ролика нет"),
        ("ERROR: [vk] x: Access restricted", "приватности"),
        ("ERROR: [youtube] x: This live event will begin in 2 hours", "Трансляция ещё не началась"),
        ("ERROR: [youtube] x: Requested format is not available", "снизить максимальное качество"),
        ("ERROR: [dailymotion] x: The extractor is attempting impersonation, but none of these impersonate targets are available: firefox", "защита от ботов"),
    ],
)
def test_known_failures_are_explained_in_russian(ydl, tmp_path, raw, expected):
    ydl.error = yt_dlp.utils.DownloadError(raw)
    with pytest.raises(DownloadError, match=expected):
        download_video_cached(URL, cache_dir=tmp_path)


RUTUBE_404 = f"ERROR: [rutube] {RUTUBE_ID}: Unable to download options JSON: HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)"


def test_a_rutube_video_behind_a_subscription_is_not_called_deleted(ydl, tmp_path, monkeypatch):
    """Так выглядят ролики PREMIER: yt-dlp видит только 404, но плеер знает, что это подписка."""
    asked = []
    monkeypatch.setattr(rutube, "paywall", lambda video_id: asked.append(video_id) or "PREMIER")
    ydl.error = yt_dlp.utils.DownloadError(RUTUBE_404)

    with pytest.raises(DownloadError) as caught:
        download_video_cached(RUTUBE_URL, cache_dir=tmp_path)

    assert asked == [RUTUBE_ID]
    message = str(caught.value)
    assert "по подписке PREMIER" in message and "скачать его нельзя" in message and "удалили" not in message


def test_the_same_explanation_is_given_when_the_link_is_first_checked(ydl, monkeypatch):
    monkeypatch.setattr(rutube, "paywall", lambda video_id: "")
    ydl.error = yt_dlp.utils.DownloadError(RUTUBE_404)

    with pytest.raises(DownloadError, match="по платной подписке"):
        fetch_video_info(RUTUBE_URL)


def test_a_rutube_404_without_a_subscription_stays_a_missing_video(ydl, tmp_path, monkeypatch):
    monkeypatch.setattr(rutube, "paywall", lambda video_id: None)
    ydl.error = yt_dlp.utils.DownloadError(RUTUBE_404)

    with pytest.raises(DownloadError, match="Такого ролика нет"):
        download_video_cached(RUTUBE_URL, cache_dir=tmp_path)


def test_only_rutube_failures_are_checked_for_a_subscription(ydl, tmp_path, monkeypatch):
    def forbidden(video_id):
        raise AssertionError("другие площадки у RuTube не спрашиваем")

    monkeypatch.setattr(rutube, "paywall", forbidden)
    ydl.error = yt_dlp.utils.DownloadError("ERROR: [youtube] x: HTTP Error 404: Not Found")

    with pytest.raises(DownloadError, match="Такого ролика нет"):
        download_video_cached(URL, cache_dir=tmp_path)


def test_other_rutube_failures_do_not_trigger_the_subscription_check(ydl, tmp_path, monkeypatch):
    def forbidden(video_id):
        raise AssertionError("лишний запрос к плееру")

    monkeypatch.setattr(rutube, "paywall", forbidden)
    ydl.error = yt_dlp.utils.DownloadError("ERROR: [rutube] x: Requested format is not available")

    with pytest.raises(DownloadError, match="снизить максимальное качество"):
        download_video_cached(RUTUBE_URL, cache_dir=tmp_path)


def test_an_unknown_failure_names_the_platform_and_hides_the_terminal_noise(ydl, tmp_path):
    ydl.error = yt_dlp.utils.DownloadError("\x1b[0;31mERROR:\x1b[0m [rutube] x: Что-то пошло не так\nвторая строка")
    with pytest.raises(DownloadError) as caught:
        download_video_cached(RUTUBE_URL, cache_dir=tmp_path)
    message = str(caught.value)
    assert "площадки RuTube" in message and "Что-то пошло не так" in message
    assert "вторая строка" not in message and "\x1b" not in message


# ---------- сведения о ролике ----------


def test_video_info_has_everything_the_ui_card_needs(ydl):
    ydl.info = video_info()
    assert fetch_video_info(URL) == {
        "platform": "youtube",
        "url": URL,
        "title": "Заголовок",
        "duration": 212,
        "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
        "uploader": "Автор",
    }
    opts = ydl.instances[0].opts
    # «плоский» разбор плейлистов: ссылка на канал не раскроется в сотни роликов
    # двух записей хватает, чтобы отличить одиночный ролик в плейлисте из одного элемента от настоящего плейлиста
    assert (opts["quiet"], opts["no_warnings"], opts["extract_flat"], opts["playlistend"]) == (True, True, "in_playlist", 2)
    assert opts["noplaylist"] is True


def test_youtube_link_is_normalised_to_the_plain_watch_address(ydl):
    ydl.info = video_info(thumbnail=None)
    info = fetch_video_info(f"https://youtu.be/{VIDEO_ID}?t=30&list=PL123")
    assert info["url"] == URL
    assert info["thumbnail"] == f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"  # превью есть всегда


def test_other_platforms_keep_the_link_as_it_was_sent(ydl):
    private = f"https://rutube.ru/video/private/{RUTUBE_ID}/?p=secret"
    ydl.info = video_info(id=RUTUBE_ID, thumbnail=None)
    info = fetch_video_info(private)
    assert info["platform"] == "rutube" and info["url"] == private and info["thumbnail"] == ""
    assert ydl.instances[0].asked == private


def test_uploader_falls_back_to_channel_and_then_to_nothing(ydl):
    ydl.info = video_info(uploader=None, channel="Канал")
    assert fetch_video_info(VK_URL)["uploader"] == "Канал"
    ydl.info = video_info(uploader=None)
    assert fetch_video_info(VK_URL)["uploader"] == ""


@pytest.mark.parametrize(
    "extra",
    [{"_type": "playlist"}, {"_type": "multi_video"}, {"entries": []}, {"entries": [{"id": "x"}, {"id": "y"}]}],
)
def test_channels_and_playlists_are_refused(ydl, extra):
    ydl.info = video_info(**extra)
    with pytest.raises(DownloadError, match="канал или плейлист"):
        fetch_video_info(RUTUBE_URL)


def test_a_playlist_of_one_video_is_that_video(ydl):
    # Первый канал отдаёт даже одиночный выпуск плейлистом из одного элемента: это обычный ролик, а не канал
    episode = video_info(id="ep1", title="Выпуск от 3 ноября", duration=3623, uploader=None, thumbnail="https://img/ep.jpg")
    ydl.info = {"_type": "playlist", "id": "page", "title": "Страница выпуска", "entries": [episode]}
    info = fetch_video_info(FIRSTTV_URL)
    assert info == {
        "platform": "firsttv",
        "url": FIRSTTV_URL,
        "title": "Выпуск от 3 ноября",  # заголовок самого ролика, а не обёртки
        "duration": 3623.0,
        "thumbnail": "https://img/ep.jpg",
        "uploader": "",
    }


def test_the_wrapper_fills_in_what_the_single_video_does_not_say(ydl):
    ydl.info = {"_type": "playlist", "title": "Название страницы", "uploader": "Первый канал", "entries": [{"duration": 600}]}
    info = fetch_video_info(FIRSTTV_URL)
    assert (info["title"], info["uploader"], info["duration"]) == ("Название страницы", "Первый канал", 600.0)


def test_a_playlist_with_a_single_live_stream_is_still_refused(ydl):
    ydl.info = {"_type": "playlist", "entries": [video_info(is_live=True)]}
    with pytest.raises(DownloadError, match="прямой эфир"):
        fetch_video_info(FIRSTTV_URL)


@pytest.mark.parametrize("extra", [{"is_live": True}, {"live_status": "is_live"}, {"live_status": "is_upcoming"}])
def test_live_streams_are_refused(ydl, extra):
    ydl.info = video_info(**extra)
    with pytest.raises(DownloadError, match="прямой эфир"):
        fetch_video_info(URL)


@pytest.mark.parametrize("duration", [None, 0, -5])
def test_unknown_length_is_refused(ydl, duration):
    ydl.info = video_info(duration=duration)
    with pytest.raises(DownloadError, match="длину ролика"):
        fetch_video_info(URL)


def test_marathons_are_refused_but_the_limit_itself_is_fine(ydl):
    ydl.info = video_info(duration=MAX_SOURCE_SECONDS + 60)
    with pytest.raises(DownloadError, match="4 ч 1 мин.*длиннее 4 часов"):
        fetch_video_info(URL)
    ydl.info = video_info(duration=MAX_SOURCE_SECONDS)
    assert fetch_video_info(URL)["duration"] == MAX_SOURCE_SECONDS


def test_an_empty_answer_is_reported_with_the_platform(ydl):
    ydl.info = None
    with pytest.raises(DownloadError, match="VK Видео не вернул сведений"):
        fetch_video_info(VK_URL)


def test_unavailable_video_is_reported_as_download_error(ydl):
    ydl.error = yt_dlp.utils.DownloadError("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable")
    with pytest.raises(DownloadError, match="недоступно"):
        fetch_video_info(URL)


def test_every_javascript_runtime_on_path_is_enabled_for_youtube_only(ydl, monkeypatch):
    # yt-dlp сам включает только deno; без JS-рантайма YouTube отдаёт урезанный набор форматов
    monkeypatch.setattr(youtube.shutil, "which", lambda name: f"/bin/{name}" if name in {"node", "bun"} else None)
    ydl.info = video_info()
    fetch_video_info(URL)
    fetch_video_info(RUTUBE_URL)
    assert ydl.instances[0].opts["js_runtimes"] == {"node": {}, "bun": {}}
    assert "js_runtimes" not in ydl.instances[1].opts


# ---------- локальные файлы ----------


def test_upload_with_a_cyrillic_name_is_stored_under_a_safe_id(tmp_path):
    upload_id, path = save_local_upload("Мой ролик.MP4", io.BytesIO(b"video-bytes"), tmp_path)
    assert len(upload_id) == 32 and set(upload_id) <= set("0123456789abcdef")
    assert path == tmp_path / f"{upload_id}.mp4"
    assert path.read_bytes() == b"video-bytes"
    assert resolve_upload(upload_id, tmp_path) == path


@pytest.mark.parametrize("filename, content", [("virus.exe", b"MZ"), ("notes.txt", b"hi"), ("empty.mp4", b"")])
def test_unsuitable_uploads_are_refused_and_leave_nothing_behind(tmp_path, filename, content):
    with pytest.raises(DownloadError):
        save_local_upload(filename, io.BytesIO(content), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("upload_id", ["", "../x", "a" * 31, "A" * 32, "g" * 32, "0" * 32])
def test_resolve_upload_refuses_anything_that_is_not_a_known_upload(tmp_path, upload_id):
    (tmp_path / ("0" * 31 + "1.mp4")).write_bytes(b"other upload")
    with pytest.raises(DownloadError):
        resolve_upload(upload_id, tmp_path)


# ---------- субтитры площадки ----------

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class _FakeYdl:
    """Подмена YoutubeDL: отдаёт заранее заданные сведения о ролике, ничего не скачивая."""

    info: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        assert download is False, "субтитры не должны тянуть само видео"
        return dict(self.info)


def _fake_response(payload: dict):
    import json as _json

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return _json.dumps(payload).encode()

    return Answer()


def test_subtitles_are_taken_from_the_platform_when_json3_exists(monkeypatch):
    from app.services import downloader

    _FakeYdl.info = {"automatic_captions": {"ru": [{"ext": "json3", "url": "https://subs.example/ru.json3"}]}}
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FakeYdl)
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen", lambda *args, **kwargs: _fake_response({"events": [{"tStartMs": 0}]})
    )

    assert downloader.fetch_subtitles(YT) == {"events": [{"tStartMs": 0}]}


def test_own_subtitles_win_over_automatic_ones(monkeypatch):
    from app.services import downloader

    _FakeYdl.info = {
        "subtitles": {"ru": [{"ext": "vtt", "url": "https://subs.example/ru.vtt"}]},
        "automatic_captions": {"ru": [{"ext": "json3", "url": "https://subs.example/auto.json3"}]},
    }
    asked: list[str] = []

    def fake_urlopen(request, timeout=0):
        asked.append(request.full_url)
        return _fake_response({"events": []})

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FakeYdl)
    monkeypatch.setattr(downloader.urllib.request, "urlopen", fake_urlopen)

    downloader.fetch_subtitles(YT)

    # своих субтитров в json3 нет — берём автоматические, но не vtt: времени по словам там нет
    assert asked == ["https://subs.example/auto.json3"]


def test_without_subtitles_the_answer_is_none(monkeypatch):
    from app.services import downloader

    _FakeYdl.info = {"subtitles": {}, "automatic_captions": {}}
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FakeYdl)
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen", lambda *a, **k: pytest.fail("нечего скачивать")
    )

    assert downloader.fetch_subtitles(YT) is None


def test_platform_failure_does_not_break_the_job(monkeypatch):
    from app.services import downloader

    class Broken(_FakeYdl):
        def extract_info(self, url, download=False):
            raise downloader.yt_dlp.utils.YoutubeDLError("площадка молчит")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", Broken)

    assert downloader.fetch_subtitles(YT) is None  # клип просто выйдет без субтитров


# ---------- скачивание только окна ----------

YT2 = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def test_a_section_is_asked_with_the_download_ranges_option(monkeypatch, tmp_path):
    """Окно просят у площадки штатным `download_ranges` — тем же механизмом, что `--download-sections`."""
    from app.services import downloader

    seen: dict = {}

    class Recording(_FakeYdl):
        def __init__(self, options, *args, **kwargs):
            seen.update(options)

        def download(self, urls):
            # площадка «отдала» файл нужного размера
            target = Path(seen["outtmpl"].replace(".%(ext)s", ".mp4"))
            target.write_bytes(b"x" * (downloader.MIN_CACHE_BYTES + 1))

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", Recording)

    result = downloader.download_section(YT2, 61.0, 106.0, "youtube-aaa", cache_dir=tmp_path)

    assert result is not None and result.name == "section_youtube-aaa_61.0-106.0.mp4"
    ranges = list(seen["download_ranges"]({"id": "x", "duration": 1000}, None))
    assert ranges == [{"start_time": 61.0, "end_time": 106.0}]


def test_a_section_already_in_cache_is_not_downloaded_again(monkeypatch, tmp_path):
    from app.services import downloader

    cached = downloader.section_path("youtube-aaa", 61.0, 106.0, tmp_path)
    cached.write_bytes(b"x" * (downloader.MIN_CACHE_BYTES + 1))
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", pytest.fail)

    assert downloader.download_section(YT2, 61.0, 106.0, "youtube-aaa", cache_dir=tmp_path) == cached


def test_a_failed_section_download_returns_none_so_the_caller_fetches_the_whole_file(monkeypatch, tmp_path):
    from app.services import downloader

    class Broken(_FakeYdl):
        def __init__(self, *args, **kwargs):
            pass

        def download(self, urls):
            raise downloader.yt_dlp.utils.YoutubeDLError("площадка не умеет секции")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", Broken)

    assert downloader.download_section(YT2, 61.0, 106.0, "youtube-aaa", cache_dir=tmp_path) is None


def test_audio_is_downloaded_with_the_audio_only_format(monkeypatch, tmp_path):
    """Звук качается отдельно и перекладывается в `audio_<ключ>.m4a` — по нему считаются окна клипов."""
    from app.services import downloader

    seen: dict = {}

    class Recording(_FakeYdl):
        def __init__(self, options, *args, **kwargs):
            seen.update(options)

        def download(self, urls):
            Path(seen["outtmpl"].replace(".%(ext)s", ".webm")).write_bytes(b"x" * (downloader.MIN_CACHE_BYTES + 1))

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", Recording)
    monkeypatch.setattr(downloader, "_remux_to_mp4", lambda source, target: target.write_bytes(b"y" * 200_000) or True)

    result = downloader.download_audio(YT2, "youtube-aaa", cache_dir=tmp_path)

    assert seen["format"].startswith("bestaudio")
    assert result == downloader.audio_path("youtube-aaa", tmp_path)
