"""Запасной роутер: всё, что не поймал никто другой. Подключается последним.

Команд в боте нет, поэтому незнакомое сообщение (стикер, случайный текст на экране настроек) не
остаётся без ответа: бот показывает меню с кнопками. Устаревшая кнопка (после перезапуска бота)
тоже ведёт в меню, а не молчит, и заодно сбрасывает диалог — иначе в нём остался бы чужой черновик.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.context import AppContext, edit_or_send, menu_view, reset_dialog, safe_answer, show

router = Router(name="fallback")


@router.message()
async def on_unknown_message(message: Message, state: FSMContext, ctx: AppContext) -> None:
    html, markup = menu_view(ctx, message.from_user.id)
    if ctx.busy_for(message.from_user.id) is None and await state.get_state() is not None:
        html = texts.NUDGE  # меню уже перед глазами у человека: коротко напоминаем про кнопки
    await show(message, html, markup)


@router.callback_query()
async def on_stale_callback(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback, texts.STALE)
    await reset_dialog(state, ctx, callback.from_user.id)
    html, markup = menu_view(ctx, callback.from_user.id)
    await edit_or_send(callback, html, markup)
