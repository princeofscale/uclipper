"""Запуск и отмена нарезки."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import (
    AppContext,
    menu_view,
    need_draft,
    replace,
    safe_answer,
    send_html,
    show,
)
from app.bot.states import Flow
from app.services import publishing

log = logging.getLogger(__name__)
router = Router(name="jobs")


@router.callback_query(F.data == "job:start")
async def cb_start(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Проверяет черновик и запускает нарезку, если бот свободен."""
    draft = await need_draft(callback, state)
    if draft is None:
        return
    user_id = callback.from_user.id
    busy = ctx.busy_for(user_id)
    if busy is not None:
        # Бот ведёт один ролик за раз. План остаётся на экране: когда бот освободится, достаточно нажать ещё раз.
        await safe_answer(callback, texts.busy_alert(busy), alert=True)
        return
    try:
        request = draft.to_request()
    except ValueError as error:
        await safe_answer(callback, str(error).split("\n")[-1], alert=True)
        return

    # Место занимаем сразу, до первого `await`: пока бот отвечает Telegram, задачи ещё нет, и без заявки двое,
    # нажавшие в один момент, прошли бы проверку оба.
    ctx.runner.claim(user_id)
    banner_seconds = ctx.banner_seconds(draft.settings.banner_id)
    fits = publishing.fits(draft.settings, banner_seconds)
    try:
        # Ответ у Telegram ровно один: либо тихо, либо предупреждением о длине (нарезку всё равно запускаем —
        # человек мог сознательно делать клип для другой площадки).
        if fits:
            await safe_answer(callback)
        else:
            await safe_answer(callback, texts.publish_warning(draft.settings, banner_seconds), alert=True)
        chat_id = callback.message.chat.id if callback.message is not None else user_id
        progress = await send_html(callback.bot, chat_id, texts.QUEUED_TEXT)
        await state.set_state(Flow.running)
        try:
            await ctx.runner.start(chat_id, user_id, request, progress)
        except Exception:  # noqa: BLE001 — не оставляем пользователя без ответа
            log.exception("Не удалось запустить задачу")
            await replace(
                progress,
                texts.error_text("Не получилось запустить нарезку. Попробуйте ещё раз."),
                keyboards.back_to_menu(),
            )
            await state.set_state(Flow.editing)
            return
    finally:
        ctx.runner.release(user_id)  # задача уже занимает место сама, а при сбое место возвращается
    # Экран с планом остаётся в чате как история, но кнопки с него убираем: нажимать их уже незачем.
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data.startswith("job:cancel:"))
async def cb_cancel(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = (callback.data or "job:cancel:").split(":", 2)[2]
    ok = await ctx.runner.cancel(job_id)
    await safe_answer(callback, "⏹ Останавливаю…" if ok else "Задача уже закончилась")


async def _has_active_job(message: Message, ctx: AppContext) -> bool:
    return bool(ctx.runner.active_for_user(message.from_user.id))


@router.message(_has_active_job)
async def on_busy_message(message: Message, ctx: AppContext) -> None:
    """Пока идёт нарезка, другие сообщения не мешают: показываем, как она продвигается, и кнопку «Остановить»."""
    html, markup = menu_view(ctx, message.from_user.id)
    await show(message, html, markup)
