"""Баннеры: экран со всеми баннерами проекта, выбор одного из них и превью — как он выглядит в клипах.

Выбор хранится в настройках человека (`UserStore`), поэтому следующий ролик сразу пойдёт с выбранным
баннером; текущий черновик обновляется на месте. Отсюда всегда один путь назад — в главное меню.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile

from app.bot import keyboards, texts
from app.bot.context import AppContext, edit_or_send, get_draft, put_draft, reset_dialog, safe_answer, update_settings
from app.services.banner_manager import BannerInfo

log = logging.getLogger(__name__)
router = Router(name="banners")


def _banner_id(data: str | None, prefix: str) -> str | None:
    """`banner:pick:TgRader-v2-16x9-vo.mp4` → имя файла; без хвоста — `None` (это главный баннер)."""
    if not data or not data.startswith(prefix):
        return None
    return data[len(prefix) :] or None


def selected_banner(ctx: AppContext, user_id: int) -> BannerInfo | None:
    """Баннер этого человека: сохранённый выбор, а если файл пропал — главный."""
    return ctx.banner(ctx.store.settings(user_id).banner_id) or ctx.banner()


async def render_banners(event: CallbackQuery, ctx: AppContext) -> None:
    banners = ctx.banners()
    current = selected_banner(ctx, event.from_user.id)
    chosen = current.id if current is not None else None
    await edit_or_send(event, texts.banners_screen(banners, chosen), keyboards.banners_screen(banners, chosen))


@router.callback_query(F.data == "screen:banners")
async def cb_banners(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)
    await render_banners(callback, ctx)


@router.callback_query(F.data.startswith("banner:pick") | F.data.startswith("banner:select"))
async def cb_pick(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Выбирает баннер: запоминает его в настройках человека и подставляет в открытый черновик."""
    banner = ctx.banner(_banner_id(callback.data, "banner:pick:")) or ctx.banner()
    if banner is None:
        await safe_answer(callback, texts.BANNER_MISSING, alert=True)
        return
    user_id = callback.from_user.id
    ctx.store.remember_settings(user_id, update_settings(ctx.store.settings(user_id), banner_id=banner.id))
    ctx.store.save()
    draft = await get_draft(state)
    if draft is not None:
        draft.settings = update_settings(draft.settings, banner_id=banner.id)
        await put_draft(state, draft)
    await safe_answer(callback, f"Теперь в клипах: {banner.name}")
    await render_banners(callback, ctx)


@router.callback_query(F.data.startswith("banner:preview"))
async def cb_preview(callback: CallbackQuery, ctx: AppContext) -> None:
    """Присылает сам баннер видео, чтобы его можно было посмотреть. Загруженный файл запоминается по file_id."""
    banner = ctx.banner(_banner_id(callback.data, "banner:preview:"))
    if banner is None:
        banner = selected_banner(ctx, callback.from_user.id)
    if banner is None:
        await safe_answer(callback, texts.BANNER_MISSING, alert=True)
        return
    await safe_answer(callback, f"Отправляю «{banner.name}»…")
    options = dict(
        caption=texts.banner_caption(banner),
        supports_streaming=True,
        duration=round(banner.duration or 0) or None,
        width=banner.width,
        height=banner.height,
    )
    chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    if ctx.banner_previews.get(banner.id):
        try:
            await callback.bot.send_video(chat_id, ctx.banner_previews[banner.id], **options)
            return
        except TelegramBadRequest:
            ctx.banner_previews.pop(banner.id, None)  # file_id протух — отправим файл заново
    try:
        sent = await callback.bot.send_video(chat_id, FSInputFile(banner.path), **options)
    except Exception:  # noqa: BLE001 — превью необязательно, экран остаётся рабочим
        log.warning("Не удалось отправить превью баннера", exc_info=True)
        await safe_answer(callback, "Не получилось отправить баннер — попробуйте позже", alert=True)
        return
    if sent.video is not None:
        ctx.banner_previews[banner.id] = sent.video.file_id
