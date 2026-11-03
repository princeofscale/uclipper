"""Загрузчик: распознавание ссылок YouTube, кеш по video_id, передача опций yt-dlp, локальные файлы.

yt-dlp подменяется фейком (в тестах нет сети); всё остальное — настоящий код: файлы, кеш, ремукс через ffmpeg.
"""
import io
import shutil
import subprocess
from pathlib import Path

import pytest
import yt_dlp

from app.services import downloader
from app.services.downloader import (
    DownloadError,
    cached_source_path,
    download_video_cached,
    extract_video_id,
    fetch_video_info,
    resolve_upload,
    save_local_upload,
)

VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


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

        def __init__(self, opts):
            self.opts = opts
            FakeYDL.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            if FakeYDL.error:
                raise FakeYDL.error
            return FakeYDL.info

        def download(self, urls):
            if FakeYDL.error:
                raise FakeYDL.error
            for event in FakeYDL.progress_events:
                for hook in self.opts.get("progress_hooks", []):
                    hook(event)
            Path(self.opts["outtmpl"].replace("%(ext)s", FakeYDL.output_ext)).write_bytes(FakeYDL.output_bytes)

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


# ---------- ссылки ----------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&t=10s",
        f"https://youtu.be/{VIDEO_ID}?t=10",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}?feature=share",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"youtube.com/watch?v={VIDEO_ID}",
        f"  https://music.youtube.com/watch?v={VIDEO_ID}  ",
    ],
)
def test_extract_video_id_understands_every_common_youtube_link(url):
    assert extract_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        f"https://example.com/watch?v={VIDEO_ID}",
        f"https://youtube.com.evil.com/watch?v={VIDEO_ID}",
        f"https://evil.com/?u=youtube.com/watch?v={VIDEO_ID}",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/",
        "https://www.youtube.com/playlist?list=PL123",
        "not a url",
        "",
    ],
)
def test_extract_video_id_rejects_everything_else(url):
    assert extract_video_id(url) is None


def test_non_youtube_links_are_refused_before_any_network_call(ydl, tmp_path):
    with pytest.raises(DownloadError):
        fetch_video_info("https://example.com/x")
    with pytest.raises(DownloadError):
        download_video_cached("https://example.com/x", cache_dir=tmp_path)
    assert ydl.instances == []


# ---------- кеш и скачивание ----------


def test_cached_video_is_returned_without_touching_yt_dlp(ydl, tmp_path):
    cached = tmp_path / f"source_{VIDEO_ID}.mp4"
    cached.write_bytes(b"c" * (200 * 1024))
    assert download_video_cached(URL, cache_dir=tmp_path) == cached
    assert cached_source_path(VIDEO_ID, tmp_path) == cached
    assert ydl.instances == []


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
    assert opts["format"] == "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best"
    assert opts["merge_output_format"] == "mp4"
    assert opts["noplaylist"] is True
    assert opts["outtmpl"] == str(tmp_path / f"source_{VIDEO_ID}.%(ext)s")
    # при одинаковом разрешении берём H.264, а не AV1: он декодируется везде и быстрее
    assert opts["format_sort"] == ["res", "vcodec:h264"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")
def test_a_non_mp4_result_ends_up_under_the_cache_name(ydl, tmp_path):
    mkv = tmp_path / "made.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-b:v", "2M", "-c:a", "aac",
         str(mkv)],
        check=True,
    )
    ydl.output_ext, ydl.output_bytes = "mkv", mkv.read_bytes()
    result = download_video_cached(URL, cache_dir=tmp_path / "cache")
    assert result == tmp_path / "cache" / f"source_{VIDEO_ID}.mp4"
    assert result.stat().st_size > 100 * 1024
    assert not (tmp_path / "cache" / f"source_{VIDEO_ID}.mkv").exists()


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


def test_a_failed_download_is_reported_in_russian(ydl, tmp_path):
    ydl.error = yt_dlp.utils.DownloadError("ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in if you've been granted access")
    with pytest.raises(DownloadError, match="(?i)приватн"):
        download_video_cached(URL, cache_dir=tmp_path)


# ---------- сведения о ролике ----------


def test_video_info_has_everything_the_ui_card_needs(ydl):
    ydl.info = {
        "id": VIDEO_ID,
        "title": "Заголовок",
        "duration": 212,
        "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
        "uploader": "Автор",
        "formats": [
            {"height": 720, "vcodec": "avc1"},
            {"height": 1080, "vcodec": "avc1"},
            {"height": 720, "vcodec": "vp9"},
            {"height": None, "vcodec": "none"},  # только звук
            {"height": 360, "vcodec": "avc1"},
        ],
    }
    assert fetch_video_info(URL) == {
        "video_id": VIDEO_ID,
        "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "title": "Заголовок",
        "duration": 212,
        "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
        "uploader": "Автор",
        "resolutions": [1080, 720, 360],
    }
    opts = ydl.instances[0].opts
    assert (opts["quiet"], opts["no_warnings"], opts["extract_flat"]) == (True, True, False)


def test_unavailable_video_is_reported_as_download_error(ydl):
    ydl.error = yt_dlp.utils.DownloadError("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable")
    with pytest.raises(DownloadError):
        fetch_video_info(URL)


def test_every_javascript_runtime_on_path_is_enabled_for_youtube(ydl, monkeypatch):
    # yt-dlp сам включает только deno; без JS-рантайма YouTube отдаёт урезанный набор форматов
    monkeypatch.setattr(downloader.shutil, "which", lambda name: f"/bin/{name}" if name in {"node", "bun"} else None)
    ydl.info = {"id": VIDEO_ID, "formats": []}
    fetch_video_info(URL)
    assert ydl.instances[0].opts["js_runtimes"] == {"node": {}, "bun": {}}


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
