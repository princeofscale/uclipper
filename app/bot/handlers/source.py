"""Приём источника: ссылка на YouTube, поиск по названию, присланный файл."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import INPUT_KEY, AppContext, put_draft, safe_answer, update_settings
from app.bot.states import Flow
from app.config import TELEGRAM_MAX_UPLOAD_MB, VIDEO_EXTS
from app.models import ClipSettings, Draft, VideoInfo
from app.services.banner_manager import list_banners
from app.services.downloader import DownloadError, extract_video_id, fetch_video_info, save_local_upload, search_videos
from app.services.video_processor import MediaError, probe_media

log = logging.getLogger(__name__)
router = Router(name="source")

# Прямая ссылка YouTube или «похоже на ссылку» (тогда честно скажем, что не подошло).
_YOUTUBE_HINT = re.compile(r"(youtu\.?\s?be|youtube\.com|youtube-nocookie\.com)", re.IGNORECASE)
_URL_HINT = re.compile(r"^(https?://|www\.)|\.[a-z]{2,}/", re.IGNORECASE)


async def _default_settings(ctx: AppContext, user_id: int) -> ClipSettings:
    """Настройки пользователя; если баннер ещё не выбран — подставляем главный баннер проекта."""
    settings = ctx.store.settings(user_id)
    if settings.banner_id is None and settings.banner_count > 0:
        banners = list_banners(ctx.settings.banners_dir)
        if banners:
            settings = update_settings(settings, banner_id=banners[0].id)
    return settings


async def accept_video(
    bot, chat_id: int, user_id: int, state: FSMContext, ctx: AppContext, info: dict
) -> None:
    """Показывает карточку найденного ролика и переводит диалог к настройкам."""
    settings = await _default_settings(ctx, user_id)
    video = VideoInfo(
        video_id=info["video_id"],
        url=info["url"],
        title=info.get("title") or "Без названия",
        duration=float(info.get("duration") or 0),
        uploader=info.get("uploader") or None,
        thumbnail=info.get("thumbnail") or None,
        resolutions=info.get("resolutions") or [],
    )
    draft = Draft.from_video(video, settings)
    await put_draft(state, draft)
    await state.set_state(Flow.editing)
    banner_name = ctx.banner_name(draft.settings.banner_id)
    static_name = ctx.banner_name(draft.settings.static_banner_id)
    # Одно сообщение: карточка ролика + все настройки + кнопка запуска. Так проще не потеряться в чате.
    await bot.send_message(
        chat_id,
        texts.settings_screen(draft, banner_name, static_name),
        reply_markup=keyboards.settings_screen(draft, banner_name, static_name),
    )


# ---------- ссылка или название ----------


async def _accepts_source(message: Message, state: FSMContext, ctx: AppContext) -> bool:
    """Текст, который надо принять как источник: ссылка или название.

    В меню-состояниях (и в пустом, после перезапуска бота) подходит любой текст. С экрана настроек
    и из списка результатов — только ссылка, и только если бот не ждёт число.
    """
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    current = await state.get_state()
    if current == Flow.running.state:
        # Состояние «идёт нарезка» само не сбрасывается: считаем занятым только пока задача реально идёт.
        return not ctx.runner.active_for_user(message.from_user.id)
    if current in (None, Flow.waiting_link.state, Flow.waiting_query.state, Flow.waiting_file.state):
        return True
    if current in (Flow.editing.state, Flow.choosing.state):
        pending = (await state.get_data()).get(INPUT_KEY)
        return not pending and bool(extract_video_id(text))
    return False


async def _not_busy(message: Message, ctx: AppContext) -> bool:
    """Файл принимаем, если у пользователя не идёт нарезка."""
    return not ctx.runner.active_for_user(message.from_user.id)


@router.message(_accepts_source, F.text)
async def on_text(message: Message, state: FSMContext, ctx: AppContext) -> None:
    text = (message.text or "").strip()
    if extract_video_id(text):
        await _from_url(message, state, ctx, text)
        return
    current = await state.get_state()
    looks_like_link = _YOUTUBE_HINT.search(text) or _URL_HINT.search(text)
    if looks_like_link and current != Flow.waiting_query.state:
        await message.answer(texts.BAD_LINK, reply_markup=keyboards.back_to_menu())
        return
    # Не ссылка — считаем, что это название для поиска.
    await _search(message, state, ctx, text)


async def _from_url(message: Message, state: FSMContext, ctx: AppContext, url: str) -> None:
    status = await message.answer(texts.FETCHING)
    try:
        info = await asyncio.to_thread(fetch_video_info, url)
    except DownloadError as error:
        await status.edit_text(texts.error_text(str(error)), reply_markup=keyboards.back_to_menu())
        return
    except Exception:  # noqa: BLE001 — сеть/yt-dlp могут упасть нестандартно
        log.exception("Не удалось получить сведения о ролике")
        await status.edit_text(texts.error_text("Не получилось получить сведения о ролике. Попробуйте ещё раз."))
        return
    await status.delete()
    await accept_video(message.bot, message.chat.id, message.from_user.id, state, ctx, info)


# ---------- поиск по названию ----------


async def _search(message: Message, state: FSMContext, ctx: AppContext, query: str) -> None:
    status = await message.answer(texts.SEARCHING.format(query=texts._escape(query)))
    try:
        results = await asyncio.to_thread(search_videos, query)
    except DownloadError as error:
        await status.edit_text(texts.error_text(str(error)), reply_markup=keyboards.back_to_menu())
        return
    except Exception:  # noqa: BLE001
        log.exception("Поиск на YouTube упал")
        await status.edit_text(texts.error_text("Поиск не удался. Попробуйте ещё раз или пришлите ссылку."))
        return
    if not results:
        await status.edit_text(
            texts.SEARCH_NOTHING.format(query=texts._escape(query)), reply_markup=keyboards.back_to_menu()
        )
        return
    await status.edit_text(texts.search_cards(results), reply_markup=keyboards.search_results(results))
    await state.update_data(search=results)
    await state.set_state(Flow.choosing)


# ---------- выбор найденного ----------


@router.callback_query(Flow.choosing, F.data.startswith("pick:"))
async def on_pick(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    data = await state.get_data()
    results = data.get("search") or []
    try:
        index = int((callback.data or "pick:-1").split(":", 1)[1])
        entry = results[index]
    except (ValueError, IndexError):
        await safe_answer(callback, "Вариант устарел — поищите ещё раз", alert=True)
        return
    await safe_answer(callback)
    if callback.message is not None:
        try:
            await callback.message.delete()
        except Exception:  # noqa: BLE001
            pass
    info = {
        "video_id": entry["video_id"],
        "url": entry["url"],
        "title": entry.get("title"),
        "duration": entry.get("duration"),
        "uploader": entry.get("uploader"),
        "thumbnail": entry.get("thumbnail"),
    }
    await accept_video(callback.bot, callback.from_user.id, callback.from_user.id, state, ctx, info)


# ---------- видеофайл ----------


@router.message(_not_busy, F.video | F.document | F.animation)
async def on_file(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Приём видеофайла: до 20 МБ (лимит облачного Bot API)."""
    media = message.video or message.document or message.animation
    if media is None:
        return
    name = getattr(message.video, "file_name", None) or getattr(media, "file_name", None) or "video.mp4"
    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_EXTS:
        await message.answer(texts.NOT_A_VIDEO, reply_markup=keyboards.back_to_menu())
        return
    if media.file_size and media.file_size > TELEGRAM_MAX_UPLOAD_MB * 1024 * 1024:
        await message.answer(texts.FILE_TOO_BIG, reply_markup=keyboards.back_to_menu())
        return

    status = await message.answer(texts.DOWNLOADING_FILE)
    uploads_dir = ctx.settings.uploads_dir
    try:
        temp = uploads_dir / f"incoming_{message.from_user.id}_{media.file_unique_id}{suffix}"
        await message.bot.download(media, destination=temp, timeout=120)
        upload_id, path = await asyncio.to_thread(_store_upload, temp, name, uploads_dir)
        info = await asyncio.to_thread(probe_media, path)
    except DownloadError as error:
        await status.edit_text(texts.error_text(str(error)), reply_markup=keyboards.back_to_menu())
        return
    except MediaError:
        await status.edit_text(
            texts.FILE_BAD.format(error="файл не читается как видео"),
            reply_markup=keyboards.back_to_menu(),
        )
        return
    except Exception:  # noqa: BLE001
        log.exception("Не удалось принять видеофайл")
        await status.edit_text(
            texts.error_text("Не получилось принять файл. Попробуйте ещё раз или пришлите ссылку на YouTube.")
        )
        return

    settings = await _default_settings(ctx, message.from_user.id)
    draft = Draft.from_upload(upload_id, Path(name).stem or "Видео", info.duration, settings)
    await put_draft(state, draft)
    await state.set_state(Flow.editing)
    await status.delete()
    await message.answer(
        texts.settings_screen(draft, ctx.banner_name(draft.settings.banner_id), None),
        reply_markup=keyboards.settings_screen(draft, ctx.banner_name(draft.settings.banner_id), None),
    )


def _store_upload(temp: Path, original_name: str, uploads_dir: Path) -> tuple[str, Path]:
    """Кладёт временный файл как обычную загрузку (`save_local_upload`) и убирает временный."""
    try:
        with temp.open("rb") as stream:
            upload_id, path = save_local_upload(original_name, stream, uploads_dir)
        return upload_id, path
    finally:
        temp.unlink(missing_ok=True)
