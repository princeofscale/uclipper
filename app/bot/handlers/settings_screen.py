"""Ручная настройка: экран со сводкой, выбор из вариантов, края ролика и ввод чисел (пресеты, «−/+», свой ввод)."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import (
    INPUT_KEY,
    AppContext,
    clear_input,
    edit_or_send,
    get_draft,
    need_draft,
    put_draft,
    safe_answer,
    update_settings,
)
from app.bot.options import CHOICES
from app.bot.states import Flow
from app.bot.steppers import STEPPERS

log = logging.getLogger(__name__)
router = Router(name="settings")


# ---------- отрисовка экранов ----------


async def _back_to_menu(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    from app.bot.handlers.common import open_menu

    await open_menu(event, state, ctx)


async def render_manual(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await _back_to_menu(event, state, ctx)
        return
    await clear_input(state)
    await state.set_state(Flow.editing)
    banner = ctx.banner(draft.settings.banner_id) or ctx.banner()
    await edit_or_send(
        event,
        texts.manual_screen(draft, banner),
        keyboards.manual_screen(
            draft, banner_available=banner is not None, banner_name=banner.name if banner is not None else None
        ),
    )


async def render_more(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _back_to_menu(event, state, ctx)
    await edit_or_send(event, texts.more_screen(draft.settings), keyboards.more_settings(draft.settings))


async def render_trim(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _back_to_menu(event, state, ctx)
    await edit_or_send(event, texts.trim_screen(draft.settings), keyboards.trim_screen(draft.settings))


async def render_stepper(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext, field: str) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _back_to_menu(event, state, ctx)
    stepper = STEPPERS[field]
    value = getattr(draft.settings, field)
    await edit_or_send(event, texts.stepper_screen(stepper, value), keyboards.stepper_screen(stepper, value))


async def render_choice(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext, field: str) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _back_to_menu(event, state, ctx)
    current = getattr(draft.settings, field)
    await edit_or_send(event, texts.choice_screen(field, current), keyboards.choice_screen(field, current))


_RENDERERS: dict[str, Callable] = {
    "manual": render_manual,
    "more": render_more,
    "trim": render_trim,
}


async def render_screen(event, state: FSMContext, ctx: AppContext, name: str) -> None:
    """Экран по короткому имени из callback_data (`manual`, `more`, `trim`)."""
    await _RENDERERS.get(name, render_manual)(event, state, ctx)


@router.callback_query(F.data == "screen:manual")
async def cb_manual(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if await get_draft(state) is None:
        await safe_answer(callback, texts.NEED_VIDEO, alert=True)
        return
    await safe_answer(callback)
    await render_manual(callback, state, ctx)


@router.callback_query(F.data == "screen:more")
async def cb_more(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if await need_draft(callback, state) is None:
        return
    await safe_answer(callback)
    await clear_input(state)
    await render_more(callback, state, ctx)


@router.callback_query(F.data == "screen:trim")
async def cb_trim(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if await need_draft(callback, state) is None:
        return
    await safe_answer(callback)
    await clear_input(state)
    await render_trim(callback, state, ctx)


# ---------- выбор из вариантов ----------


@router.callback_query(F.data.startswith("choose:"))
async def cb_choose(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    field = (callback.data or "choose:").split(":", 1)[1]
    if field not in CHOICES:
        await safe_answer(callback, "Эту настройку пока нельзя изменить", alert=True)
        return
    if await need_draft(callback, state) is None:
        return
    await safe_answer(callback)
    await clear_input(state)
    await state.set_state(Flow.editing)
    await render_choice(callback, state, ctx, field)


@router.callback_query(F.data.startswith("opt:"))
async def cb_option(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """`opt:<поле>:<значение>` — применяет выбранный вариант и возвращает на экран, откуда пришли."""
    parts = (callback.data or "").split(":", 2)
    choice = CHOICES.get(parts[1]) if len(parts) == 3 else None
    if choice is None or not choice.has(parts[2]):
        await safe_answer(callback, "Этого варианта больше нет — выберите из кнопок", alert=True)
        return
    draft = await need_draft(callback, state)
    if draft is None:
        return
    value: object = int(parts[2]) if parts[2].isdigit() else parts[2]
    draft.settings = update_settings(draft.settings, **{choice.field: value})
    await put_draft(state, draft)
    await safe_answer(callback, f"{choice.option(parts[2]).label}")
    await render_screen(callback, state, ctx, "more" if choice.back == "screen:more" else "manual")


# ---------- переключатели ----------

_TOGGLES = {
    "auto_trim": ("Автообрезка включена", "Автообрезка выключена"),
    "banner_sound": ("Звук баннера включён", "Звук баннера выключен"),
    "enable_sfx": ("Сигнал перед баннером включён", "Сигнал перед баннером выключен"),
    "preview": ("Черновик 540p: рендер вчетверо быстрее", "Полное качество 1080p"),
}


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """`toggle:<поле>[:<экран>]` — экран подсказывает, куда вернуться после переключения."""
    parts = (callback.data or "").split(":")
    field = parts[1] if len(parts) > 1 else ""
    screen = parts[2] if len(parts) > 2 else "manual"
    if field != "banner" and field not in _TOGGLES:
        await safe_answer(callback)
        return
    draft = await need_draft(callback, state)
    if draft is None:
        return
    if field == "banner":  # баннер — это «ноль показов» или больше; включаем с одним показом на клип
        banner = ctx.banner(draft.settings.banner_id) or ctx.banner()
        if banner is None:
            await safe_answer(callback, texts.BANNER_MISSING, alert=True)
            return
        enable = draft.settings.banner_count == 0
        draft.settings = update_settings(
            draft.settings, banner_id=banner.id, banner_count=1 if enable else 0
        )
        toast = f"Баннер включён: {banner.name}" if enable else "Баннер выключен"
    else:
        value = not getattr(draft.settings, field)
        draft.settings = update_settings(draft.settings, **{field: value})
        toast = _TOGGLES[field][0 if value else 1]
    await put_draft(state, draft)
    await safe_answer(callback, toast)
    await render_screen(callback, state, ctx, screen)


# ---------- числа: пресеты, «−/+», свой ввод ----------


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    field = (callback.data or "edit:").split(":", 1)[1]
    if field not in STEPPERS:
        await safe_answer(callback, "Эту настройку пока нельзя изменить", alert=True)
        return
    if await need_draft(callback, state) is None:
        return
    await safe_answer(callback)
    await clear_input(state)
    await state.set_state(Flow.editing)
    await render_stepper(callback, state, ctx, field)


async def _set_number(callback: CallbackQuery, state: FSMContext, ctx: AppContext, field: str, value: float) -> None:
    stepper = STEPPERS[field]
    draft = await need_draft(callback, state)
    if draft is None:
        return
    value = stepper.clamp(value)
    draft.settings = update_settings(draft.settings, **{field: value})
    await put_draft(state, draft)
    await safe_answer(callback)
    await render_stepper(callback, state, ctx, field)


@router.callback_query(F.data.regexp(r"^set:\w+:-?\d+(\.\d+)?$"))
async def cb_set(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    _, field, raw = (callback.data or "").split(":")
    if field in STEPPERS:
        await _set_number(callback, state, ctx, field, float(raw))
    else:
        await safe_answer(callback)


@router.callback_query(F.data.regexp(r"^step:\w+:-?\d+(\.\d+)?$"))
async def cb_step(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    _, field, raw = (callback.data or "").split(":")
    stepper = STEPPERS.get(field)
    draft = await get_draft(state)
    if stepper is None or draft is None:
        await safe_answer(callback, texts.LOST_DRAFT if draft is None else None, alert=draft is None)
        return
    await _set_number(callback, state, ctx, field, getattr(draft.settings, field) + float(raw))


@router.callback_query(F.data.startswith("type:"))
async def cb_type(callback: CallbackQuery, state: FSMContext) -> None:
    """«Ввести своё число»: следующее сообщение с числом станет значением поля."""
    field = (callback.data or "type:").split(":", 1)[1]
    stepper = STEPPERS.get(field)
    draft = await need_draft(callback, state)
    if stepper is None or draft is None:
        return
    await safe_answer(callback)
    await state.set_state(Flow.editing)
    await state.update_data({INPUT_KEY: field})
    await edit_or_send(
        callback,
        texts.stepper_prompt(stepper, getattr(draft.settings, field)),
        keyboards.cancel_only(f"edit:{field}"),
    )


_NUMBER = re.compile(r"^\s*-?\d+([.,]\d+)?\s*$")


async def _has_pending_input(message: Message, state: FSMContext) -> bool:
    data = await state.get_data()
    return bool(data.get(INPUT_KEY)) and await state.get_state() == Flow.editing.state


@router.message(_has_pending_input, F.text)
async def on_number(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Принимает число для поля, выбранного кнопкой «Ввести своё число»."""
    from app.bot.context import show

    field = (await state.get_data()).get(INPUT_KEY)
    stepper = STEPPERS.get(field)
    draft = await get_draft(state)
    if stepper is None or draft is None:
        await clear_input(state)
        await _back_to_menu(message, state, ctx)
        return
    cancel = keyboards.cancel_only(f"edit:{field}")
    text = (message.text or "").strip()
    if not _NUMBER.match(text):
        await show(message, texts.stepper_invalid(stepper), cancel)
        return
    value = float(text.replace(",", "."))
    if not stepper.valid(value):
        await show(message, texts.stepper_invalid(stepper, value), cancel)
        return
    try:
        draft.settings = update_settings(draft.settings, **{field: value})
    except ValueError:
        # Сообщение pydantic пользователю не показываем: там английский текст и служебные детали.
        log.warning("Настройка %s не приняла значение %r", field, value)
        await show(message, texts.stepper_invalid(stepper, value), cancel)
        return
    await put_draft(state, draft)
    await clear_input(state)
    await render_stepper(message, state, ctx, field)
