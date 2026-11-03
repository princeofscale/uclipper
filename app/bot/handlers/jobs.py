"""Запуск и отмена нарезки."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import AppContext, need_draft, safe_answer
from app.bot.states import Flow

log = logging.getLogger(__name__)
router = Router(name="jobs")


@router.callback_query(F.data == "job:start")
async def cb_start(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Проверяет черновик и ставит задачу в очередь."""
    draft = await need_draft(callback, state)
    if draft is None:
        return
    if ctx.runner.active_for_user(callback.from_user.id):
        await safe_answer(callback, "Предыдущая нарезка ещё идёт — дождитесь её или нажмите «Остановить»", alert=True)
        return
    try:
        request = draft.to_request()
    except ValueError as error:
        message = str(error).split("\n")[-1]
        await safe_answer(callback, message, alert=True)
        return

    await safe_answer(callback)
    progress = await callback.bot.send_message(callback.from_user.id, texts.QUEUED_TEXT)
    await state.set_state(Flow.running)
    try:
        await ctx.runner.start(callback.from_user.id, callback.from_user.id, request, progress)
    except Exception:  # noqa: BLE001 — не оставляем пользователя без ответа
        log.exception("Не удалось запустить задачу")
        await progress.edit_text(
            texts.error_text("Не получилось запустить нарезку. Попробуйте ещё раз."),
            reply_markup=keyboards.back_to_menu(),
        )
        await state.set_state(Flow.editing)
        return
    # Экран настроек под сообщением-прогрессом больше не нужен: убираем кнопки, чтобы не нажимали зря.
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data.startswith("job:cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    job_id = (callback.data or "job:cancel:").split(":", 2)[2]
    ok = await ctx.runner.cancel(job_id)
    await safe_answer(callback, "⏹ Останавливаю…" if ok else "Задача уже закончилась")


async def _has_active_job(message: Message, ctx: AppContext) -> bool:
    return bool(ctx.runner.active_for_user(message.from_user.id))


@router.message(_has_active_job)
async def on_busy_message(message: Message) -> None:
    """Пока идёт нарезка, другие сообщения не мешают: подсказываем, как остановить."""
    await message.answer("⏳ Сейчас идёт нарезка. Дождитесь результата или нажмите «⏹ Остановить» под прогрессом.")
