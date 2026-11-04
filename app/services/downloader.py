"""Загрузка видео: ссылки на площадки через yt-dlp (кеш по ключу ролика) и импорт локальных файлов."""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import BinaryIO, Callable

import yt_dlp

from app.config import MAX_SOURCE_SECONDS, MIN_CACHE_BYTES, VIDEO_EXTS, Settings
from app.services.platforms import YOUTUBE, Platform, Source, identify, titles
from app.services.youtube import js_runtimes

_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TEMP_SUFFIXES = frozenset({".part", ".ytdl", ".temp", ".tmp"})
_LIVE_STATES = frozenset({"is_live", "is_upcoming"})

# (подстрока в тексте ошибки yt-dlp, объяснение для пользователя); текст ошибки сравнивается в нижнем регистре
_ERROR_HINTS = (
    ("private video", "Видео приватное — площадка не отдаёт его без входа в аккаунт."),
    ("access restricted", "Видео закрыто настройками приватности — площадка не отдаёт его без входа в аккаунт."),
    ("sign in to confirm", "YouTube просит подтвердить, что вы не бот. Попробуйте позже или загрузите видео файлом."),
    ("confirm your age", "Видео с возрастным ограничением — площадка не отдаёт его без входа в аккаунт."),
    ("members-only", "Видео только для подписчиков канала — площадка не отдаёт его без входа в аккаунт."),
    ("video unavailable", "Видео недоступно: оно удалено или закрыто для вашего региона."),
    ("does not exist", "Такого ролика нет: возможно, его удалили."),
    ("http error 404", "Такого ролика нет: возможно, его удалили."),
    ("requested format is not available", "Для этого ролика нет подходящего формата — попробуйте снизить максимальное качество."),
    ("will begin in", "Трансляция ещё не началась."),
    ("impersonat", "Площадка сейчас не пускает сервер (защита от ботов) — попробуйте ещё раз через пару минут."),
)


class DownloadError(RuntimeError):
    """Не удалось получить видео. Текст — по-русски, его увидит пользователь."""


# ---------------------------------------------------------------------------
# Ссылки на площадки
# ---------------------------------------------------------------------------


def require_source(url: str) -> Source:
    source = identify(url)
    if source is None:
        raise DownloadError(f"Эта ссылка мне незнакома. Я работаю с: {titles()} — пришлите ссылку на конкретный ролик.")
    return source


def _base_options(platform: Platform) -> dict:
    options: dict = {"noplaylist": True, "socket_timeout": 20}
    if platform is YOUTUBE:  # без JS-рантайма YouTube отдаёт урезанный набор форматов
        options["js_runtimes"] = js_runtimes()
    return options


def _explain(error: Exception, platform: Platform) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error)).strip()  # без ANSI-цветов yt-dlp
    lowered = text.lower()
    for needle, message in _ERROR_HINTS:
        if needle in lowered:
            return message
    first_line = text.removeprefix("ERROR: ").splitlines()[0] if text else "неизвестная ошибка"
    return f"Не удалось получить видео с площадки {platform.title}: {first_line}"


def _format_selector(max_height: int) -> str:
    """Лучшее видео не выше `max_height`: mp4 + m4a (как у YouTube), потом готовый mp4, потом любые видео и звук.

    `height<=?` пропускает форматы без указанной высоты: у части площадок (например, у Coub-вставок на OK) её нет.
    """
    return (
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/best[height<={max_height}][ext=mp4]"
        f"/bestvideo[height<=?{max_height}]+bestaudio"
        f"/best[height<=?{max_height}]"
        "/best"
    )


def _hours_text(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours} ч {rest // 60} мин" if hours else f"{rest // 60} мин"


def fetch_video_info(url: str) -> dict:
    """Метаданные ролика без скачивания: `platform, url, title, duration, thumbnail, uploader`.

    Отказывает понятным текстом, если по ссылке не один ролик (плейлист, канал), это прямой эфир, длину
    узнать не вышло или ролик длиннее `MAX_SOURCE_SECONDS`.
    """
    source = require_source(url)
    platform = source.platform
    options = _base_options(platform) | {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": 1}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source.url, download=False)
    except yt_dlp.utils.YoutubeDLError as error:
        raise DownloadError(_explain(error, platform)) from error
    if not info:
        raise DownloadError(f"{platform.title} не вернул сведений о ролике.")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise DownloadError("Это ссылка на канал или плейлист. Пришлите ссылку на конкретный ролик.")
    if info.get("is_live") or info.get("live_status") in _LIVE_STATES:
        raise DownloadError("Это прямой эфир — нарезать можно только готовую запись. Пришлите ссылку на запись.")
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise DownloadError("Не удалось узнать длину ролика — пришлите ссылку на обычное видео или файл.")
    if duration > MAX_SOURCE_SECONDS:
        raise DownloadError(
            f"Ролик идёт {_hours_text(duration)} — длиннее {MAX_SOURCE_SECONDS // 3600} часов, "
            "для бота это слишком тяжело. Пришлите ролик покороче."
        )

    found_id = info.get("id") or ""
    if platform is YOUTUBE:  # канонический адрес: без списков воспроизведения и отметок времени
        canonical = f"https://www.youtube.com/watch?v={found_id}"
        thumbnail = info.get("thumbnail") or f"https://i.ytimg.com/vi/{found_id}/hqdefault.jpg"
    else:
        canonical, thumbnail = source.url, info.get("thumbnail") or ""
    return {
        "platform": platform.id,
        "url": canonical,
        "title": info.get("title") or "",
        "duration": duration,
        "thumbnail": thumbnail,
        "uploader": info.get("uploader") or info.get("channel") or "",
    }


def cached_source_path(key: str, cache_dir: Path) -> Path | None:
    """Путь к ролику в кеше, если он уже скачан целиком (`source_<ключ>.mp4` крупнее 100 КБ)."""
    path = cache_dir / f"source_{key}.mp4"
    return path if path.is_file() and path.stat().st_size > MIN_CACHE_BYTES else None


def download_video_cached(
    url: str,
    max_height: int = 1080,
    *,
    cache_dir: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Скачивает ролик в `workspace/cache/source_<ключ>.mp4`; если он там уже есть — возвращает сразу.

    `on_progress` получает долю 0..1 (видео и аудио качаются отдельными файлами: каждый — половина шкалы).
    """
    source = require_source(url)
    key = source.key
    cache_dir = cache_dir or Settings.default().cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cached_source_path(key, cache_dir)
    if cached is not None:
        return cached

    seen_files: list[str] = []
    reported = 0.0

    def on_event(event: dict) -> None:
        nonlocal reported
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
        reported = min(1.0, (min(seen_files.index(name), 1) + part) / 2)
        on_progress(reported)

    options = _base_options(source.platform) | {
        "format": _format_selector(max_height),
        "format_sort": ["res", "vcodec:h264"],  # то же разрешение — H.264 раньше AV1 (быстрее и совместимее)
        "merge_output_format": "mp4",
        "outtmpl": str(cache_dir / f"source_{key}.%(ext)s"),
        "quiet": False,
        "noprogress": True,
        "progress_hooks": [on_event],
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([source.url])
    except yt_dlp.utils.YoutubeDLError as error:
        raise DownloadError(_explain(error, source.platform)) from error
    if on_progress is not None and reported < 1.0:
        on_progress(1.0)  # файл был один (HLS, клип): шкала «два файла» до конца не дошла сама
    return _finalize(key, cache_dir)


def _finalize(key: str, cache_dir: Path) -> Path:
    """Гарантирует, что результат лежит именно в `source_<ключ>.mp4` (формат `/best` может отдать mkv/webm)."""
    target = cache_dir / f"source_{key}.mp4"
    if target.is_file() and target.stat().st_size > MIN_CACHE_BYTES:
        return target
    leftovers = sorted(
        path
        for path in cache_dir.glob(f"source_{key}.*")
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
