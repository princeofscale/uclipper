"""Доставка готовых роликов в Telegram: видео по одному, сжатие слишком больших, ZIP-архив.

Лимиты обычного бота: отдать файл можно до 50 МБ. Если клип больше — перекодируем его в 720×1280
с разумным битрейтом: качество остаётся приемлемым, а вес падает в разы.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge
from aiogram.types import FSInputFile

from app.bot import texts
from app.config import (
    REENCODE_AUDIO_BITRATE,
    REENCODE_CRF,
    REENCODE_HEIGHT,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MAX_DOWNLOAD_MB,
)
from app.models import ClipResult, JobState

log = logging.getLogger(__name__)

MAX_BYTES = TELEGRAM_MAX_DOWNLOAD_MB * 1024 * 1024
SHRINK_PREFIX = "tg_"


@dataclass(frozen=True)
class DeliveryReport:
    """Что удалось отправить."""

    sent: int  # роликов ушло
    failed: int  # роликов не ушло
    archive_sent: bool


def _size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _fits(path: Path, limit: int = MAX_BYTES) -> bool:
    return 0 < _size(path) <= limit


def shrink_clip(source: Path, target: Path, *, max_height: int = REENCODE_HEIGHT) -> Path:
    """Перекодирует клип в `max_height` (по короткой стороне) и возвращает путь к новому файлу.

    Ширина/высота определяются по исходнику: вертикальный остаётся вертикальным.
    """
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-vf", f"scale=-2:{max_height}:force_original_aspect_ratio=decrease",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(REENCODE_CRF), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", REENCODE_AUDIO_BITRATE,
        "-movflags", "+faststart", str(target),
    ]  # fmt: skip
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg не смог сжать ролик: {done.stderr.strip()[-300:] or 'неизвестная ошибка'}")
    return target


def prepare_for_telegram(path: Path, work_dir: Path) -> tuple[Path, bool]:
    """Возвращает `(файл для отправки, пришлось ли сжимать)`. Сжатый файл кладётся рядом с пометкой `tg_`."""
    if _fits(path):
        return path, False
    target = work_dir / f"{SHRINK_PREFIX}{path.name}"
    try:
        shrink_clip(path, target)
    except RuntimeError as error:
        log.warning("Не удалось сжать %s: %s", path, error)
        return path, False
    return target, True


async def send_clip(
    bot: Bot,
    chat_id: int,
    clip: ClipResult,
    path: Path,
    work_dir: Path,
    *,
    total: int,
) -> bool:
    """Отправляет один ролик. `False` — не получилось даже после сжатия."""
    file, shrunk = prepare_for_telegram(path, work_dir)
    caption = f"🎬 <b>Клип {clip.index} из {total}</b> · ⏱ {texts.duration_text(clip.duration)}"
    if shrunk:
        caption += "\n<i>Сжал до 720p, чтобы влез в лимит Telegram.</i>"
    caption = caption[:TELEGRAM_CAPTION_LIMIT]
    try:
        await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(file, filename=file.name),
            caption=caption,
            supports_streaming=True,
            duration=round(clip.duration),
        )
        return True
    except TelegramEntityTooLarge:
        log.warning("Видео %s всё ещё больше лимита Telegram (%s байт)", file, _size(file))
        return False
    except TelegramBadRequest as error:
        log.warning("Telegram не принял видео %s: %s", file, error)
        # Последняя попытка: отправить как документ — иногда видео-метод капризничает на нестандартных файлах.
        try:
            await bot.send_document(chat_id=chat_id, document=FSInputFile(file, filename=file.name))
            return True
        except (TelegramBadRequest, TelegramEntityTooLarge):
            return False


async def send_archive(bot: Bot, chat_id: int, archive: Path) -> bool:
    """Отправляет ZIP, если он влезает в лимит."""
    if not archive.is_file() or not _fits(archive):
        return False
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(archive, filename=archive.name),
            caption=f"📦 <b>Все клипы одним архивом</b> · {archive.stat().st_size / 1024 / 1024:.1f} МБ",
        )
        return True
    except (TelegramBadRequest, TelegramEntityTooLarge) as error:
        log.warning("Архив %s не ушёл: %s", archive, error)
        return False


async def deliver_results(
    bot: Bot,
    chat_id: int,
    state: JobState,
    manager,
    *,
    already_sent: set[int] | None = None,
) -> DeliveryReport:
    """Отправляет клипы задачи и архив. Возвращает отчёт; на диске ничего не удаляет сам.

    `already_sent` — индексы роликов, которые бот уже прислал пользователю во время рендера:
    второй раз их не отправляем.
    """
    job_dir = manager.job_dir(state.id)
    sent = failed = 0
    total = max(state.total_clips, len(state.clips))
    skip = already_sent or set()
    for clip in state.clips:
        if clip.index in skip:
            sent += 1
            continue
        path = manager.clip_path(state.id, clip)
        if path is None:
            failed += 1
            continue
        ok = await send_clip(bot, chat_id, clip, path, job_dir, total=total)
        sent += int(ok)
        failed += int(not ok)
    archive = manager.zip_path(state.id)
    archive_sent = await send_archive(bot, chat_id, archive) if archive is not None else False
    return DeliveryReport(sent=sent, failed=failed, archive_sent=archive_sent)


def cleanup_job(manager, job_id: str) -> None:
    """Убирает папку задачи с диска после доставки."""
    try:
        manager.discard(job_id)
    except OSError:
        log.warning("Не удалось удалить папку задачи %s", job_id, exc_info=True)
