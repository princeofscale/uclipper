"""Общие экраны: главное меню, справка, статистика и запросы источника.

Команд в боте нет. Единственное исключение — `/start`: его шлёт сам Telegram, когда человек нажимает
«Старт» у пустого чата, и обойти это нельзя. Он нигде не показывается и не упоминается: тот же экран
открывает кнопка «🏠 В начало» и любое сообщение, которое бот не понял.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import AppContext, clear_input, edit_or_send, safe_answer, show
from app.bot.states import Flow

log = logging.getLogger(__name__)
router = Router(name="common")


async def open_menu(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Главное меню. Черновик не теряется; во время нарезки диалог остаётся заблокированным."""
    await clear_input(state)
    if await state.get_state() != Flow.running.state or not ctx.runner.active_for_user(event.from_user.id):
        await state.set_state(Flow.waiting_link)
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, texts.START, keyboards.main_menu())
    else:
        await show(event, texts.START, keyboards.main_menu())


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Скрытый вход для кнопки «Старт» в Telegram."""
    await open_menu(message, state, ctx)


@router.callback_query(F.data == "screen:menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await open_menu(callback, state, ctx)


@router.callback_query(F.data == "screen:help")
async def cb_help(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    await edit_or_send(callback, texts.HELP, keyboards.back_to_menu())


@router.callback_query(F.data == "screen:stats")
async def cb_stats(callback: CallbackQuery, ctx: AppContext) -> None:
    await safe_answer(callback)
    clips, jobs = ctx.store.stats(callback.from_user.id)
    await edit_or_send(callback, texts.stats_text(clips, jobs, callback.from_user.id), keyboards.back_to_menu())


async def _ask(callback: CallbackQuery, state: FSMContext, flow, text: str) -> None:
    await safe_answer(callback)
    await clear_input(state)
    await state.set_state(flow)
    await edit_or_send(callback, text, keyboards.back_to_menu())


@router.callback_query(F.data == "ask:link")
async def cb_ask_link(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, Flow.waiting_link, texts.REQUEST_LINK)


@router.callback_query(F.data == "ask:query")
async def cb_ask_query(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, Flow.waiting_query, texts.REQUEST_QUERY)


@router.callback_query(F.data == "ask:file")
async def cb_ask_file(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, Flow.waiting_file, texts.REQUEST_FILE)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    """Кнопка-подпись (например, текущее значение между «−» и «+»)."""
    await safe_answer(callback)
