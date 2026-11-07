"""Режим нарезки: 🤖 Авто (план по длине ролика и цели) или 🛠 Вручную.

После выбора ролика человек попадает на экран выбора режима. «Авто» считает план и показывает его
с кнопкой запуска; «Вручную» открывает все настройки. Между режимами можно ходить туда-сюда:
«Подправить вручную» оставляет значения автоплана, а возврат к «Авто» считает план заново.
"""
from __future__ import annotations

import asyncio
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
from app.models import ClipSettings, CutMode, Draft, Goal
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


def mode_view(draft: Draft, ctx: AppContext, *, user_id: int | None = None):
    """Экран выбора режима: текст и кнопки. Прошлую нарезку предлагаем, только если она была."""
    repeat = bool(user_id is not None and ctx.history.remembered(user_id))
    return texts.mode_screen(draft, repeat=repeat), keyboards.mode_picker(repeat=repeat)


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
    html, markup = mode_view(draft, ctx, user_id=getattr(event.from_user, "id", None))
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


@router.callback_query(F.data == "mode:repeat")
async def cb_repeat(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """«🔁 Как в прошлый раз»: берём число клипов и длину из истории и показываем ручную настройку.

    В историю пишется длина готового клипа (вместе с паузами под баннер), поэтому паузы вычитаем:
    иначе каждый повтор удлинял бы клип на длину баннера.
    """
    draft = await need_draft(callback, state)
    if draft is None:
        return
    remembered = await asyncio.to_thread(ctx.history.remembered, callback.from_user.id)
    if not remembered:
        await safe_answer(callback, "Прошлых нарезок ещё нет — это первая", alert=True)
        return
    banner_id = remembered.get("banner") or draft.settings.banner_id
    banners = int(remembered.get("banner_count") or 0)
    pause = ctx.banner_seconds(banner_id) or 0.0
    clean = float(remembered.get("clip_len") or 0) - (banners * pause if banners else 0.0)
    try:
        draft.settings = ClipSettings(
            **{
                **draft.settings.model_dump(),
                "num_clips": int(remembered.get("of") or draft.settings.num_clips),
                "clip_duration": max(2.0, min(180.0, round(clean, 2))) if clean > 0 else draft.settings.clip_duration,
                "banner_id": banner_id if ctx.banner(banner_id) else draft.settings.banner_id,
                "banner_count": banners,
            }
        )
    except ValueError:  # данные истории не подошли (старый формат) — просто показываем ручную настройку
        log.warning("Не удалось применить прошлые настройки из истории", exc_info=True)
    draft.mode = CutMode.MANUAL
    await put_draft(state, draft)
    await safe_answer(callback, "Взял числа из прошлой нарезки")
    await render_manual(callback, state, ctx)


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
