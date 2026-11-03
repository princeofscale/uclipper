"""Баннеры: выбор из списка, включение/отключение, загрузка своего баннера."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import AppContext, edit_or_send, get_draft, put_draft, safe_answer, update_settings
from app.bot.states import Flow
from app.config import BANNER_EXTS
from app.services.banner_manager import BannerError, list_banners, save_uploaded_banner

log = logging.getLogger(__name__)
router = Router(name="banners")

# Что показать у каждого баннера в списке
_MEDIA_HINTS = {
    "image": "картинка",
    "gif": "анимация",
    "video": "видео со звуком",
}


def _banner_catalog(ctx: AppContext) -> str:
    banners = list_banners(ctx.settings.banners_dir)
    if not banners:
        return texts.BANNER_EMPTY
    lines = ["<b>Баннеры</b>", ""]
    for banner in banners:
        size = f"{banner.width}×{banner.height}"
        extra = []
        if banner.duration:
            extra.append(f"{banner.duration:.1f} с")
        if banner.has_audio:
            extra.append("со звуком")
        if banner.role == "strip":
            extra.append("полоса под постоянный")
        meta = " · ".join([size, *_MEDIA_HINTS.get(banner.media, banner.media), *extra])
        lines.append(f"• <b>{texts._escape(banner.name)}</b>\n  <i>{meta}</i>")
    lines.append("")
    lines.append("Выберите баннер кнопкой ниже или загрузите свой: картинку (png, jpg, webp, gif) "
                 "или видео (mp4, mov, webm) до 20 секунд.")
    return "\n".join(lines)


async def render_banners(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await edit_or_send(event, texts.START, keyboards.main_menu())
        return
    banners = list_banners(ctx.settings.banners_dir)
    await edit_or_send(
        event,
        _banner_catalog(ctx),
        keyboards.banner_picker(banners, draft.settings.banner_id),
    )


@router.message(Command("banners"))
async def cmd_banners(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """/banners — каталог баннеров с выбором."""
    await render_banners(message, state, ctx)


@router.callback_query(F.data == "screen:banners")
async def cb_banners(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await render_banners(callback, state, ctx)


@router.callback_query(F.data.startswith("banner:"))
async def cb_choose_banner(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Выбор баннера для всплывающих показов."""
    banner_id = (callback.data or "banner:").split(":", 1)[1]
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    banner = next((item for item in ctx.banners() if item.id == banner_id), None)
    if banner is None:
        await safe_answer(callback, "Этого баннера больше нет — обновите список", alert=True)
        return
    changes: dict = {"banner_id": banner.id}
    if draft.settings.banner_count == 0:
        changes["banner_count"] = 2  # выбрали баннер — покажем его дважды на клип
    draft.settings = update_settings(draft.settings, **changes)
    await put_draft(state, draft)
    await safe_answer(callback, f"Баннер: {banner.name}")
    await render_settings_after(callback, state, ctx)


@router.callback_query(F.data.startswith("static:"))
async def cb_choose_static(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Выбор баннера-полосы для постоянного показа (в поле кадра)."""
    banner_id = (callback.data or "static:").split(":", 1)[1]
    if banner_id in ("on", "off"):
        return
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    banner = next((item for item in ctx.banners() if item.id == banner_id), None)
    if banner is None:
        await safe_answer(callback, "Этого баннера больше нет — обновите список", alert=True)
        return
    draft.settings = update_settings(draft.settings, static_banner_id=banner.id)
    await put_draft(state, draft)
    await safe_answer(callback, f"Постоянный баннер: {banner.name}")
    await render_settings_after(callback, state, ctx)


async def render_settings_after(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    from app.bot.handlers.settings_screen import render_settings

    await render_settings(event, state, ctx)


# ---------- загрузка своего баннера ----------


@router.callback_query(F.data == "ask:banner")
async def cb_ask_banner(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(Flow.waiting_banner)
    await edit_or_send(callback, texts.REQUEST_BANNER, keyboards.cancel_only())


@router.message(Flow.waiting_banner, F.photo | F.document | F.video | F.animation)
async def on_banner_file(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Принимает картинку/видео как свой баннер и сразу подставляет его в настройки."""
    media, filename = _banner_media(message)
    if media is None:
        await message.answer(texts.BANNER_FAILED.format(error="не вижу файла"), reply_markup=keyboards.cancel_only())
        return
    status = await message.answer("Принимаю баннер…")
    # Скачиваем файл и потом разбираем его в потоке: Pillow/FFmpeg блокируют цикл событий.
    temp = ctx.settings.uploads_dir / f"banner_{message.from_user.id}_{media.file_unique_id}{Path(filename).suffix}"
    try:
        await message.bot.download(media, destination=temp, timeout=120)
        data = await asyncio.to_thread(temp.read_bytes)
        info = await asyncio.to_thread(save_uploaded_banner, filename, data, ctx.settings.banners_dir)
    except BannerError as error:
        await status.edit_text(
            texts.BANNER_FAILED.format(error=texts._escape(str(error))), reply_markup=keyboards.cancel_only()
        )
        return
    except Exception:  # noqa: BLE001
        log.exception("Не удалось принять баннер")
        await status.edit_text(texts.error_text("Не получилось принять баннер. Попробуйте другой файл."))
        return
    finally:
        temp.unlink(missing_ok=True)

    draft = await get_draft(state)
    if draft is not None:
        changes: dict = {"banner_id": info.id}
        if draft.settings.banner_count == 0:
            changes["banner_count"] = 2
        draft.settings = update_settings(draft.settings, **changes)
        await put_draft(state, draft)
    await state.set_state(Flow.editing)
    await status.delete()
    await message.answer(texts.BANNER_SAVED.format(name=texts._escape(info.name)))
    if draft is not None:
        from app.bot.handlers.settings_screen import render_settings

        await render_settings(message, state, ctx)


def _banner_media(message: Message):
    """Файл баннера из сообщения: фото берём максимальным размером, остальное — как прислали."""
    if message.photo:
        return message.photo[-1], f"photo_{message.photo[-1].file_unique_id}.jpg"
    for candidate in (message.document, message.video, message.animation):
        if candidate is None:
            continue
        name = getattr(candidate, "file_name", None) or f"banner_{candidate.file_unique_id}"
        suffix = Path(name).suffix.lower()
        if suffix not in BANNER_EXTS:
            return None, name
        return candidate, name
    return None, ""


@router.message(Flow.waiting_banner)
async def on_banner_wrong(message: Message) -> None:
    """Что-то не то вместо баннера."""
    await message.answer(texts.REQUEST_BANNER, reply_markup=keyboards.cancel_only())
