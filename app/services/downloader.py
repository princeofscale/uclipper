"""Загрузка видео: YouTube через yt-dlp (кеш по video_id) и импорт локальных файлов."""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import parse_qs, urlparse

import yt_dlp

from app.config import MIN_CACHE_BYTES, VIDEO_EXTS, Settings

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
)
_PATH_PREFIXES = frozenset({"shorts", "embed", "live", "v"})
_TEMP_SUFFIXES = frozenset({".part", ".ytdl", ".temp", ".tmp"})

# (подстрока в тексте ошибки yt-dlp, объяснение для пользователя)
_ERROR_HINTS = (
    ("private video", "Видео приватное — YouTube не отдаёт его без входа в аккаунт."),
    ("sign in to confirm", "YouTube просит подтвердить, что вы не бот. Попробуйте позже или загрузите видео файлом."),
    ("confirm your age", "Видео с возрастным ограничением — YouTube не отдаёт его без входа в аккаунт."),
    ("members-only", "Видео только для подписчиков канала — YouTube не отдаёт его без входа в аккаунт."),
    ("video unavailable", "Видео недоступно: оно удалено или закрыто для вашего региона."),
    ("requested format is not available", "Для этого ролика нет подходящего формата — попробуйте снизить максимальное качество."),
    ("will begin in", "Трансляция ещё не началась."),
)


class DownloadError(RuntimeError):
    """Не удалось получить видео. Текст — по-русски, его увидит пользователь."""


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def extract_video_id(url: str) -> str | None:
    """11-символьный id ролика из ссылки YouTube (watch, youtu.be, shorts, embed, live) или `None`."""
    text = (url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return None

    host = parsed.hostname or ""
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
                candidate = parts[1]
    return candidate if candidate and _ID_PATTERN.match(candidate) else None


def require_video_id(url: str) -> str:
    video_id = extract_video_id(url)
    if video_id is None:
        raise DownloadError("Это не похоже на ссылку на YouTube-ролик. Вставьте ссылку вида https://www.youtube.com/watch?v=…")
    return video_id


def _js_runtimes() -> dict[str, dict]:
    """JS-рантаймы для YouTube. yt-dlp сам включает только deno, поэтому перечисляем все найденные на PATH."""
    found = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    return found or {"deno": {}}


def _explain(error: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error)).strip()  # без ANSI-цветов yt-dlp
    lowered = text.lower()
    for needle, message in _ERROR_HINTS:
        if needle in lowered:
            return message
    first_line = text.removeprefix("ERROR: ").splitlines()[0] if text else "неизвестная ошибка"
    return f"Не удалось получить видео с YouTube: {first_line}"


def fetch_video_info(url: str) -> dict:
    """Метаданные ролика без скачивания: `video_id, url, title, duration, thumbnail, uploader, resolutions`."""
    video_id = require_video_id(url)
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": True,
        "js_runtimes": _js_runtimes(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as error:
        raise DownloadError(_explain(error)) from error
    if not info:
        raise DownloadError("YouTube не вернул сведений о ролике.")

    heights = {
        fmt["height"] for fmt in info.get("formats") or [] if fmt.get("height") and fmt.get("vcodec") != "none"
    }
    found_id = info.get("id") or video_id
    return {
        "video_id": found_id,
        "url": f"https://www.youtube.com/watch?v={found_id}",
        "title": info.get("title") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{found_id}/hqdefault.jpg",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "resolutions": sorted(heights, reverse=True),
    }


def search_videos(query: str, limit: int = 5) -> list[dict]:
    """Ищет ролики на YouTube по названию — без скачивания, только карточки.

    Возвращает список `{video_id, title, duration, uploader, url, thumbnail}`. Пустой список — ничего не нашли.
    Ссылки на превью строятся по id: так они есть всегда, даже если yt-dlp не отдал миниатюры в «плоском» поиске.
    """
    text = " ".join((query or "").split())
    if len(text) < 2:
        raise DownloadError("Слишком короткое название — напишите хотя бы пару слов.")
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "js_runtimes": _js_runtimes(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(f"ytsearch{limit}:{text}", download=False)
    except yt_dlp.utils.YoutubeDLError as error:
        raise DownloadError(_explain(error)) from error

    found = []
    for entry in (result or {}).get("entries") or []:
        video_id = entry.get("id")
        if not video_id or not _ID_PATTERN.match(video_id):
            continue
        found.append(
            {
                "video_id": video_id,
                "title": entry.get("title") or "Без названия",
                "duration": float(entry.get("duration") or 0),
                "uploader": entry.get("uploader") or entry.get("channel") or "",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
        )
    if not found:
        raise DownloadError(f"По запросу «{text}» ничего не нашлось — попробуйте переформулировать или пришлите ссылку.")
    return found


def cached_source_path(video_id: str, cache_dir: Path) -> Path | None:
    """Путь к ролику в кеше, если он уже скачан целиком (`source_<id>.mp4` крупнее 100 КБ)."""
    path = cache_dir / f"source_{video_id}.mp4"
    return path if path.is_file() and path.stat().st_size > MIN_CACHE_BYTES else None


def download_video_cached(
    url: str,
    max_height: int = 1080,
    *,
    cache_dir: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Скачивает ролик в `workspace/cache/source_<id>.mp4`; если он там уже есть — возвращает сразу.

    `on_progress` получает долю 0..1 (видео и аудио качаются отдельными файлами: каждый — половина шкалы).
    """
    video_id = require_video_id(url)
    cache_dir = cache_dir or Settings.default().cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cached_source_path(video_id, cache_dir)
    if cached is not None:
        return cached

    seen_files: list[str] = []

    def on_event(event: dict) -> None:
        if on_progress is None:
            return
        name = event.get("filename") or ""
        if name not in seen_files:
            seen_files.append(name)
        if event.get("status") == "finished":
            part = 1.0
        else:
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            part = min(1.0, (event.get("downloaded_bytes") or 0) / total) if total else 0.0
        on_progress(min(1.0, (min(seen_files.index(name), 1) + part) / 2))

    options = {
        "format": f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]/best",
        "format_sort": ["res", "vcodec:h264"],  # то же разрешение — H.264 раньше AV1 (быстрее и совместимее)
        "merge_output_format": "mp4",
        "outtmpl": str(cache_dir / f"source_{video_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "noprogress": True,
        "progress_hooks": [on_event],
        "js_runtimes": _js_runtimes(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.YoutubeDLError as error:
        raise DownloadError(_explain(error)) from error
    return _finalize(video_id, cache_dir)


def _finalize(video_id: str, cache_dir: Path) -> Path:
    """Гарантирует, что результат лежит именно в `source_<id>.mp4` (формат `/best` может отдать mkv/webm)."""
    target = cache_dir / f"source_{video_id}.mp4"
    if target.is_file() and target.stat().st_size > MIN_CACHE_BYTES:
        return target
    leftovers = sorted(
        path
        for path in cache_dir.glob(f"source_{video_id}.*")
        if path != target and path.is_file() and path.suffix.lower() not in _TEMP_SUFFIXES
    )
    for source in leftovers:
        if _remux_to_mp4(source, target):
            source.unlink(missing_ok=True)
            return target
    raise DownloadError("Скачивание завершилось, но файл с видео не появился — попробуйте ещё раз.")


def _remux_to_mp4(source: Path, target: Path) -> bool:
    """Сначала без перекодирования, при неудаче — с перекодированием в H.264/AAC."""
    for codec_args in (["-c", "copy"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"]):
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), *codec_args, "-movflags", "+faststart", str(target)]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise DownloadError("Не найден ffmpeg — установите FFmpeg и добавьте его в PATH.") from error
        if result.returncode == 0 and target.is_file() and target.stat().st_size > MIN_CACHE_BYTES:
            return True
        target.unlink(missing_ok=True)
    return False


# ---------------------------------------------------------------------------
# Локальные файлы
# ---------------------------------------------------------------------------


def save_local_upload(filename: str, stream: BinaryIO, uploads_dir: Path) -> tuple[str, Path]:
    """Сохраняет загруженный видеофайл как `<upload_id>.<расширение>` и возвращает `(upload_id, путь)`.

    Имя файла пользователя на диск не попадает — только расширение; так кириллица и `../` ничего не ломают.
    """
    extension = Path(filename).suffix.lower()
    if extension not in VIDEO_EXTS:
        raise DownloadError("Поддерживаются видеофайлы .mp4, .mov и .mkv.")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    target = uploads_dir / f"{upload_id}{extension}"
    try:
        with target.open("wb") as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)
        if target.stat().st_size == 0:
            raise DownloadError("Файл пустой.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return upload_id, target


def resolve_upload(upload_id: str, uploads_dir: Path) -> Path:
    """Путь к ранее загруженному файлу. Принимает только id в формате uuid4-hex — никаких путей."""
    if not _UPLOAD_ID_PATTERN.match(upload_id or ""):
        raise DownloadError("Некорректный идентификатор загрузки — загрузите видео заново.")
    for path in uploads_dir.glob(f"{upload_id}.*"):
        if path.suffix.lower() in VIDEO_EXTS and path.is_file():
            return path
    raise DownloadError("Загруженный файл не найден — загрузите видео заново.")
