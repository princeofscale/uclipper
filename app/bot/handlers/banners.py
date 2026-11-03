"""Баннеры: каталог, выбор, загрузка своего. Работает и с открытым роликом, и без него.

Без ролика выбор сохраняется как баннер по умолчанию — он подставится в следующий ролик.
Баннер-«полоса» (широкий, низкий) становится постоянным баннером в поле кадра, остальные —
всплывающими.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import (
    AppContext,
    clear_input,
    edit_or_send,
    get_draft,
    put_draft,
    safe_answer,
    update_settings,
)
from app.bot.states import Flow
from app.config import BANNER_EXTS
from app.models import ClipSettings
from app.services.banner_manager import BannerError, BannerInfo, list_banners, save_uploaded_banner

log = logging.getLogger(__name__)
router = Router(name="banners")

_MEDIA_HINTS = {"image": "картинка", "gif": "анимация", "video": "видео"}


def _banner_catalog(banners: list[BannerInfo]) -> str:
    if not banners:
        return texts.BANNER_EMPTY
    lines = ["🎬 <b>Баннеры</b>", ""]
    for banner in banners:
        meta = [f"{banner.width}×{banner.height}", _MEDIA_HINTS.get(banner.media, banner.media)]
        if banner.duration:
            meta.append(f"{banner.duration:.1f} с")
        if banner.has_audio:
            meta.append("со звуком")
        if banner.role == "strip":
            meta.append("постоянный")
        lines.append(f"• <b>{texts._escape(banner.name)}</b>\n  <i>{' · '.join(meta)}</i>")
    lines.append("")
    lines.append(
        "<blockquote>Нажмите на баннер, чтобы выбрать его. Полоса (▬) станет постоянным баннером, "
        "остальные — всплывающими. Свой можно загрузить: картинка, GIF или видео до 20 секунд.</blockquote>"
    )
    return "\n".join(lines)


async def render_banners(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    settings = draft.settings if draft is not None else ctx.store.settings(event.from_user.id)
    banners = list_banners(ctx.settings.banners_dir)
    await edit_or_send(
        event,
        _banner_catalog(banners),
        keyboards.banner_picker(
            banners, (settings.banner_id, settings.static_banner_id), with_draft=draft is not None
        ),
    )


def _apply_banner(settings: ClipSettings, banner: BannerInfo) -> tuple[ClipSettings, str]:
    """Новые настройки после выбора баннера и подпись для всплывающей подсказки."""
    if banner.role == "strip":
        if settings.static_banner_id == banner.id:  # повторное нажатие снимает постоянный баннер
            return update_settings(settings, static_banner_id=None), "Постоянный баннер убран"
        return update_settings(settings, static_banner_id=banner.id), f"Постоянный баннер: {banner.name}"
    changes: dict = {"banner_id": banner.id}
    if settings.banner_count == 0:
        changes["banner_count"] = 2  # выбрали баннер — покажем его дважды на клип
    return update_settings(settings, **changes), f"Баннер: {banner.name}"


@router.callback_query(F.data == "screen:banners")
async def cb_banners(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await clear_input(state)
    await render_banners(callback, state, ctx)


@router.callback_query(F.data.startswith("banner:"))
async def cb_choose_banner(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    banner_id = (callback.data or "banner:").split(":", 1)[1]
    banner = next((item for item in ctx.banners() if item.id == banner_id), None)
    if banner is None:
        await safe_answer(callback, "Этого баннера больше нет — обновите список", alert=True)
        await render_banners(callback, state, ctx)
        return
    draft = await get_draft(state)
    if draft is not None:
        draft.settings, toast = _apply_banner(draft.settings, banner)
        await put_draft(state, draft)
        await safe_answer(callback, toast)
        from app.bot.handlers.settings_screen import render_settings

        await render_settings(callback, state, ctx)
        return
    # Ролика ещё нет: запоминаем выбор как баннер по умолчанию и остаёмся в каталоге.
    settings, toast = _apply_banner(ctx.store.settings(callback.from_user.id), banner)
    ctx.store.remember_settings(callback.from_user.id, settings)
    await safe_answer(callback, toast)
    await render_banners(callback, state, ctx)


# ---------- загрузка своего баннера ----------


@router.callback_query(F.data == "ask:banner")
async def cb_ask_banner(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await clear_input(state)
    await state.set_state(Flow.waiting_banner)
    await edit_or_send(callback, texts.REQUEST_BANNER, keyboards.cancel_only("screen:banners"))


@router.message(Flow.waiting_banner, F.photo | F.document | F.video | F.animation)
async def on_banner_file(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Принимает картинку/видео как свой баннер и сразу подставляет его."""
    media, filename = _banner_media(message)
    cancel = keyboards.cancel_only("screen:banners")
    if media is None:
        await message.answer(texts.BANNER_FAILED.format(error="не вижу подходящего файла"), reply_markup=cancel)
        return
    status = await message.answer("⏳ Принимаю баннер…")
    # Скачиваем файл и потом разбираем его в потоке: Pillow/FFmpeg блокируют цикл событий.
    temp = ctx.settings.uploads_dir / f"banner_{message.from_user.id}_{media.file_unique_id}{Path(filename).suffix}"
    try:
        await message.bot.download(media, destination=temp, timeout=120)
        data = await asyncio.to_thread(temp.read_bytes)
        info = await asyncio.to_thread(save_uploaded_banner, filename, data, ctx.settings.banners_dir)
    except BannerError as error:
        await status.edit_text(texts.BANNER_FAILED.format(error=texts._escape(str(error))), reply_markup=cancel)
        return
    except Exception:  # noqa: BLE001
        log.exception("Не удалось принять баннер")
        await status.edit_text(texts.error_text("Не получилось принять баннер. Попробуйте другой файл."), reply_markup=cancel)
        return
    finally:
        temp.unlink(missing_ok=True)

    banner = next((item for item in ctx.banners() if item.id == info.id), info)
    draft = await get_draft(state)
    if draft is not None:
        draft.settings, _ = _apply_banner(draft.settings, banner)
        await put_draft(state, draft)
    else:
        settings, _ = _apply_banner(ctx.store.settings(message.from_user.id), banner)
        ctx.store.remember_settings(message.from_user.id, settings)
    await status.delete()
    await message.answer(texts.BANNER_SAVED.format(name=texts._escape(info.name)))
    if draft is not None:
        from app.bot.handlers.settings_screen import render_settings

        await render_settings(message, state, ctx)
    else:
        await state.set_state(Flow.waiting_link)
        await render_banners(message, state, ctx)


def _banner_media(message: Message):
    """Файл баннера из сообщения: фото берём максимальным размером, остальное — как прислали."""
    if message.photo:
        return message.photo[-1], f"photo_{message.photo[-1].file_unique_id}.jpg"
    for candidate, is_video in ((message.document, False), (message.video, True), (message.animation, True)):
        if candidate is None:
            continue
        name = getattr(candidate, "file_name", None) or f"banner_{candidate.file_unique_id}"
        if is_video and not Path(name).suffix:
            name += ".mp4"  # Telegram часто не присылает имя у видео — это всегда mp4
        if Path(name).suffix.lower() not in BANNER_EXTS:
            return None, name
        return candidate, name
    return None, ""


@router.message(Flow.waiting_banner)
async def on_banner_wrong(message: Message) -> None:
    """Что-то не то вместо баннера."""
    await message.answer(texts.REQUEST_BANNER, reply_markup=keyboards.cancel_only("screen:banners"))
