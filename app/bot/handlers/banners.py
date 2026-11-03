"""Баннеры: экран с единственным баннером проекта (TGrader) и превью, как он выглядит в клипах.

Отсюда всегда один путь назад — в главное меню: экран открывается только из него, а меню сбрасывает
диалог, поэтому «Назад» никогда не заводит в чужие настройки.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile

from app.bot import keyboards, texts
from app.bot.context import AppContext, edit_or_send, reset_dialog, safe_answer

log = logging.getLogger(__name__)
router = Router(name="banners")


async def render_banners(event: CallbackQuery, ctx: AppContext) -> None:
    banner = ctx.banner()
    await edit_or_send(event, texts.banners_screen(banner), keyboards.banners_screen(banner.name if banner else None))


@router.callback_query(F.data == "screen:banners")
async def cb_banners(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)
    await render_banners(callback, ctx)


@router.callback_query(F.data == "banner:select")
async def cb_select(callback: CallbackQuery) -> None:
    """Единственный баннер уже выбран — кнопка лишь подтверждает это."""
    await safe_answer(callback, "TGrader уже подключён — он вклеивается в каждый клип 🎬")


@router.callback_query(F.data == "banner:preview")
async def cb_preview(callback: CallbackQuery, ctx: AppContext) -> None:
    """Присылает сам баннер видео, чтобы его можно было посмотреть. Загруженный файл запоминается по file_id."""
    banner = ctx.banner()
    if banner is None:
        await safe_answer(callback, "Файла баннера нет", alert=True)
        return
    await safe_answer(callback, "Отправляю баннер…")
    caption = texts.BANNER_PREVIEW_CAPTION.format(seconds=texts.seconds_text(round(banner.duration or 0, 1)))
    options = dict(
        caption=caption,
        supports_streaming=True,
        duration=round(banner.duration or 0) or None,
        width=banner.width,
        height=banner.height,
    )
    chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    if ctx.banner_preview_id:
        try:
            await callback.bot.send_video(chat_id, ctx.banner_preview_id, **options)
            return
        except TelegramBadRequest:
            ctx.banner_preview_id = None  # file_id протух — отправим файл заново
    try:
        sent = await callback.bot.send_video(chat_id, FSInputFile(banner.path), **options)
    except Exception:  # noqa: BLE001 — превью необязательно, экран остаётся рабочим
        log.warning("Не удалось отправить превью баннера", exc_info=True)
        await safe_answer(callback, "Не получилось отправить баннер — попробуйте позже", alert=True)
        return
    if sent.video is not None:
        ctx.banner_preview_id = sent.video.file_id
