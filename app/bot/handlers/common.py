"""Общие экраны: главное меню, справка, статистика и запросы источника.

Команд в боте нет. Единственное исключение — `/start`: его шлёт сам Telegram, когда человек нажимает
«Старт» у пустого чата, и обойти это нельзя. Он нигде не показывается и не упоминается. Любая
«команда» (сообщение, которое начинается с «/») и кнопка «🏠 В начало» делают одно и то же: сбрасывают
диалог (черновик, ожидание ввода, списки) и открывают главное меню.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import AppContext, edit_or_send, reset_dialog, safe_answer
from app.bot.states import Flow

log = logging.getLogger(__name__)
router = Router(name="common")


async def open_menu(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Главное меню с чистого листа. Идущая нарезка не прерывается."""
    await reset_dialog(state, ctx, event.from_user.id)
    await edit_or_send(event, texts.START, keyboards.main_menu())


@router.message(F.text.startswith("/"))
async def on_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """`/start` и любая другая «команда»: диалог обновляется, человек видит меню."""
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


async def _ask(callback: CallbackQuery, state: FSMContext, ctx: AppContext, flow, html: str) -> None:
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)
    await state.set_state(flow)
    await edit_or_send(callback, html, keyboards.back_to_menu())


@router.callback_query(F.data == "ask:link")
async def cb_ask_link(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await _ask(callback, state, ctx, Flow.waiting_link, texts.REQUEST_LINK)


@router.callback_query(F.data == "ask:query")
async def cb_ask_query(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await _ask(callback, state, ctx, Flow.waiting_query, texts.REQUEST_QUERY)


@router.callback_query(F.data == "ask:file")
async def cb_ask_file(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await _ask(callback, state, ctx, Flow.waiting_file, texts.REQUEST_FILE)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    """Кнопка-подпись (например, текущее значение между «−» и «+»)."""
    await safe_answer(callback)
