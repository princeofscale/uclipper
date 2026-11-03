"""Общие команды: /start, /help, /new, /menu, /settings, /mystats и отмена."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import AppContext, drop_draft, edit_or_send, get_draft, safe_answer, show
from app.bot.states import Flow

log = logging.getLogger(__name__)
router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """/start: сбрасывает диалог и показывает, что можно прислать."""
    await state.clear()
    await state.set_state(Flow.waiting_link)
    await show(message, texts.START, keyboards.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await show(message, texts.HELP, keyboards.back_to_menu())


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    """Новый ролик: черновик сбрасывается, настройки пользователя остаются."""
    await state.clear()
    await state.set_state(Flow.waiting_link)
    await show(message, texts.NEW)


@router.message(Command("menu", "settings"))
async def cmd_menu(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """/menu — главное меню; /settings — сразу экран настроек."""
    draft = await get_draft(state)
    if draft is None:
        await show(message, texts.START, keyboards.main_menu())
        return
    await state.set_state(Flow.editing)
    banner_name = ctx.banner_name(draft.settings.banner_id)
    static_name = ctx.banner_name(draft.settings.static_banner_id)
    await show(
        message,
        texts.settings_screen(draft, banner_name, static_name),
        keyboards.settings_screen(draft, banner_name, static_name),
    )


@router.message(Command("mystats"))
async def cmd_stats(message: Message, ctx: AppContext) -> None:
    clips, jobs = ctx.store.stats(message.from_user.id)
    await show(message, texts.stats_text(clips, jobs, None), keyboards.back_to_menu())


@router.callback_query(F.data == "screen:menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """"В начало" — главное меню без потери черновика."""
    await safe_answer(callback)
    await edit_or_send(callback, texts.START, keyboards.main_menu())


@router.callback_query(F.data == "screen:stats")
async def cb_stats(callback: CallbackQuery, ctx: AppContext) -> None:
    await safe_answer(callback)
    clips, jobs = ctx.store.stats(callback.from_user.id)
    await edit_or_send(callback, texts.stats_text(clips, jobs, None), keyboards.back_to_menu())


@router.callback_query(F.data == "ask:link")
async def cb_ask_link(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(Flow.waiting_link)
    await edit_or_send(callback, texts.REQUEST_LINK, keyboards.back_to_menu())


@router.callback_query(F.data == "ask:query")
async def cb_ask_query(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(Flow.waiting_query)
    await edit_or_send(callback, texts.REQUEST_QUERY, keyboards.back_to_menu())


@router.callback_query(F.data == "ask:file")
async def cb_ask_file(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(Flow.waiting_file)
    await edit_or_send(callback, texts.REQUEST_FILE, keyboards.back_to_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    """Кнопка-заглушка (например, подпись выбранного баннера)."""
    await safe_answer(callback)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Отмена: если идёт задача — просим остановиться, иначе сбрасываем диалог."""
    job_id = ctx.runner.active_for_user(message.from_user.id)
    if job_id:
        await ctx.runner.cancel(job_id)
        await show(message, "Останавливаю задачу…")
        return
    await state.clear()
    await state.set_state(Flow.waiting_link)
    await show(message, "Отменил. Пришлите ссылку, название или файл — начнём заново.", keyboards.main_menu())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Мелочь для отладки: chat_id и user_id (пригодится для логов)."""
    await message.answer(
        f"chat_id: <code>{message.chat.id}</code>\nuser_id: <code>{message.from_user.id}</code>",
    )


async def reset_and_prompt(message: Message, state: FSMContext) -> None:
    """После ошибки возвращает пользователя к вводу ссылки."""
    await state.set_state(Flow.waiting_link)
    await drop_draft(state)
