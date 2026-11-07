"""Режим нарезки: 🤖 Авто (план по длине ролика и цели) или 🛠 Вручную.

После выбора ролика человек попадает на экран выбора режима. «Авто» считает план и показывает его
с кнопкой запуска; «Вручную» открывает все настройки. Между режимами можно ходить туда-сюда:
«Подправить вручную» оставляет значения автоплана, а возврат к «Авто» считает план заново.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import (
    AppContext,
    clear_input,
    edit_or_send,
    get_draft,
    need_draft,
    put_draft,
    safe_answer,
)
from app.bot.handlers.settings_screen import render_manual
from app.bot.states import Flow
from app.models import CutMode, Draft, Goal
from app.services.autoplan import AutoPlan, plan_auto

log = logging.getLogger(__name__)
router = Router(name="modes")


def make_plan(draft: Draft, ctx: AppContext) -> AutoPlan:
    return plan_auto(
        draft.duration, draft.goal, draft.settings, banner_seconds=ctx.banner_seconds(draft.settings.banner_id)
    )


def draft_banner(draft: Draft, ctx: AppContext):
    """Баннер этого черновика: выбранный в нём, а если файл пропал — главный."""
    return ctx.banner(draft.settings.banner_id) or ctx.banner()


def mode_view(draft: Draft):
    """Экран выбора режима: текст и кнопки."""
    return texts.mode_screen(draft), keyboards.mode_picker()


def auto_view(draft: Draft, ctx: AppContext):
    """Считает автоплан, кладёт его в черновик и возвращает экран: текст и кнопки."""
    plan = make_plan(draft, ctx)
    draft.settings = plan.settings
    draft.mode = CutMode.AUTO
    return (
        texts.auto_screen(draft, plan, draft_banner(draft, ctx)),
        keyboards.auto_screen(draft.goal, plan.settings.num_clips),
    )


async def render_mode(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        from app.bot.handlers.common import open_menu

        await open_menu(event, state, ctx)
        return
    await clear_input(state)
    await state.set_state(Flow.editing)
    html, markup = mode_view(draft)
    await edit_or_send(event, html, markup)


async def render_auto(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        from app.bot.handlers.common import open_menu

        await open_menu(event, state, ctx)
        return
    html, markup = auto_view(draft, ctx)
    await put_draft(state, draft)
    await clear_input(state)
    await state.set_state(Flow.editing)
    await edit_or_send(event, html, markup)


@router.callback_query(F.data == "screen:mode")
async def cb_mode(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if await need_draft(callback, state) is None:
        return
    await safe_answer(callback)
    await render_mode(callback, state, ctx)


@router.callback_query(F.data == "mode:auto")
async def cb_auto(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if await need_draft(callback, state) is None:
        return
    await safe_answer(callback)
    await render_auto(callback, state, ctx)


@router.callback_query(F.data == "mode:manual")
async def cb_manual(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await need_draft(callback, state)
    if draft is None:
        return
    await safe_answer(callback)
    draft.mode = CutMode.MANUAL
    await put_draft(state, draft)
    await render_manual(callback, state, ctx)


@router.callback_query(F.data.startswith("goal:"))
async def cb_goal(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await need_draft(callback, state)
    if draft is None:
        return
    try:
        draft.goal = Goal((callback.data or "goal:").split(":", 1)[1])
    except ValueError:
        await safe_answer(callback, "Такой цели нет — выберите из кнопок", alert=True)
        return
    await safe_answer(callback)
    await put_draft(state, draft)
    await render_auto(callback, state, ctx)
