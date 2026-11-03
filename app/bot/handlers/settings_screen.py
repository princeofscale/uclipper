"""Экран настроек: кнопки-значения, «Ещё», обрезка и выбор чисел (пресеты, «−/+», свой ввод)."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from enum import Enum
from typing import Any

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
from app.bot.states import Flow
from app.bot.steppers import STEPPERS
from app.models import BannerMode, BannerPosition, FrameMode, SlicingMode, StaticPosition

log = logging.getLogger(__name__)
router = Router(name="settings")

BANNER_COUNT_STEPS = (0, 1, 2, 3, 4, 5)
BANNER_DURATION_STEPS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0, 15.0, 20.0)
BANNER_SCALE_STEPS = (50, 60, 70, 75, 80, 85, 90, 95)
STATIC_SCALE_STEPS = (40, 50, 60, 70, 80, 85, 90, 95, 100)
MAX_HEIGHT_STEPS = (360, 480, 720, 1080, 1440, 2160)


def next_value(steps: tuple, current: Any) -> Any:
    """Следующее значение по кругу. Если текущего в списке нет — ближайшее большее (или первое)."""
    try:
        index = steps.index(current)
    except ValueError:
        greater = [value for value in steps if value > current]
        return greater[0] if greater else steps[0]
    return steps[(index + 1) % len(steps)]


def next_member(enum_type: type[Enum], current: Enum) -> Enum:
    values = list(enum_type)
    return values[(values.index(current) + 1) % len(values)]


# ---------- отрисовка экранов ----------


async def render_settings(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        from app.bot.handlers.common import open_menu

        await open_menu(event, state, ctx)
        return
    await clear_input(state)
    await state.set_state(Flow.editing)
    banner_name = ctx.banner_name(draft.settings.banner_id)
    static_name = ctx.banner_name(draft.settings.static_banner_id)
    await edit_or_send(
        event,
        texts.settings_screen(draft, banner_name, static_name),
        keyboards.settings_screen(draft, banner_name, static_name),
    )


async def render_more(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await render_settings(event, state, ctx)
    await edit_or_send(event, texts.more_screen(draft.settings), keyboards.more_settings(draft.settings))


async def render_trim(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await render_settings(event, state, ctx)
    await edit_or_send(event, texts.trim_screen(draft.settings), keyboards.trim_screen(draft.settings))


async def render_stepper(event: CallbackQuery | Message, state: FSMContext, ctx: AppContext, field: str) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await render_settings(event, state, ctx)
    stepper = STEPPERS[field]
    value = getattr(draft.settings, field)
    await edit_or_send(event, texts.stepper_screen(stepper, value), keyboards.stepper_screen(stepper, value))


_RENDERERS: dict[str, Callable] = {
    "settings": render_settings,
    "more": render_more,
    "trim": render_trim,
}


async def render_screen(event, state: FSMContext, ctx: AppContext, name: str) -> None:
    """Экран по короткому имени из callback_data (`settings`, `more`, `trim`)."""
    await _RENDERERS.get(name, render_settings)(event, state, ctx)


@router.callback_query(F.data == "screen:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if await get_draft(state) is None:
        await safe_answer(callback, texts.NEED_VIDEO, alert=True)
        return
    await safe_answer(callback)
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "screen:more")
async def cb_more(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await clear_input(state)
    await render_more(callback, state, ctx)


@router.callback_query(F.data == "screen:trim")
async def cb_trim(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await clear_input(state)
    await render_trim(callback, state, ctx)


# ---------- значения «по кругу» ----------

_ENUM_CYCLES: dict[str, tuple[type[Enum], Callable[[Any], str]]] = {
    "slicing_mode": (
        SlicingMode,
        lambda v: "Раскладка: " + {SlicingMode.EVENLY_SPACED: "равномерно по всему ролику", SlicingMode.SEQUENTIAL: "подряд с самого начала"}[v],
    ),
    "frame_mode": (
        FrameMode,
        lambda v: "Кадр: " + {
            FrameMode.BLUR_PAD: "вертикальный, на размытом фоне",
            FrameMode.CENTER_CROP: "вертикальный кроп по центру",
            FrameMode.ORIGINAL: "горизонтальный 16:9 без обрезки",
        }[v],
    ),
    "banner_mode": (
        BannerMode,
        lambda v: "Видео замирает, поверх баннер" if v is BannerMode.FREEZE else "Баннер поверх идущего видео",
    ),
    "banner_position": (BannerPosition, lambda v: f"Позиция: {texts.POSITION_LABELS[v]}"),
    "static_position": (StaticPosition, lambda v: f"Постоянный баннер: {texts.STATIC_POSITION_LABELS[v]}"),
}

_STEP_CYCLES: dict[str, tuple[tuple, Callable[[Any], str]]] = {
    "banner_count": (BANNER_COUNT_STEPS, lambda v: "Баннер выключен" if v == 0 else f"Показов на клип: {v}"),
    "banner_duration": (BANNER_DURATION_STEPS, lambda v: f"Пауза под баннер: {v:g} с"),
    "banner_scale": (BANNER_SCALE_STEPS, lambda v: f"Масштаб баннера: {v}% ширины кадра"),
    "static_scale": (STATIC_SCALE_STEPS, lambda v: f"Масштаб полосы: {v}%"),
    "max_height": (MAX_HEIGHT_STEPS, lambda v: f"Качество: до {v}p"),
}


@router.callback_query(F.data.startswith("cycle:"))
async def cb_cycle(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    field = (callback.data or "cycle:").split(":", 1)[1]
    draft = await need_draft(callback, state)
    if draft is None:
        return
    settings = draft.settings
    if field == "banner_state":  # баннер выключен → включаем с одним показом на клип
        changes, toast = {"banner_count": 1}, "Баннер включён"
    elif field in _ENUM_CYCLES:
        enum_type, describe = _ENUM_CYCLES[field]
        value = next_member(enum_type, getattr(settings, field))
        changes, toast = {field: value}, describe(value)
    elif field in _STEP_CYCLES:
        steps, describe = _STEP_CYCLES[field]
        value = next_value(steps, getattr(settings, field))
        changes, toast = {field: value}, describe(value)
    else:
        await safe_answer(callback)
        return
    draft.settings = update_settings(settings, **changes)
    await put_draft(state, draft)
    await safe_answer(callback, toast)
    await render_screen(callback, state, ctx, "more" if field == "max_height" else "settings")


_TOGGLES = {
    "auto_trim": ("Автообрезка включена", "Автообрезка выключена"),
    "banner_sound": ("Звук баннера включён", "Звук баннера выключен"),
    "enable_sfx": ("Звук появления включён", "Звук появления выключен"),
}


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """`toggle:<поле>[:<экран>]` — экран подсказывает, куда вернуться после переключения."""
    parts = (callback.data or "").split(":")
    field = parts[1] if len(parts) > 1 else ""
    screen = parts[2] if len(parts) > 2 else "settings"
    if field not in _TOGGLES:
        await safe_answer(callback)
        return
    draft = await need_draft(callback, state)
    if draft is None:
        return
    value = not getattr(draft.settings, field)
    draft.settings = update_settings(draft.settings, **{field: value})
    await put_draft(state, draft)
    await safe_answer(callback, _TOGGLES[field][0 if value else 1])
    await render_screen(callback, state, ctx, screen)


# ---------- постоянный баннер ----------


@router.callback_query(F.data == "static:on")
async def cb_static_on(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Включает постоянный баннер первой найденной полосой; другую можно выбрать в «Баннерах»."""
    draft = await need_draft(callback, state)
    if draft is None:
        return
    strips = [banner for banner in ctx.banners() if banner.role == "strip"]
    if not strips:
        await safe_answer(callback, "Полос для постоянного баннера нет — загрузите свою картинку", alert=True)
        return
    draft.settings = update_settings(draft.settings, static_banner_id=strips[0].id)
    await put_draft(state, draft)
    await safe_answer(callback, f"Постоянный баннер: {strips[0].name}")
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "static:off")
async def cb_static_off(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await need_draft(callback, state)
    if draft is None:
        return
    draft.settings = update_settings(draft.settings, static_banner_id=None)
    await put_draft(state, draft)
    await safe_answer(callback, "Постоянный баннер убран")
    await render_settings(callback, state, ctx)


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
    field = (await state.get_data()).get(INPUT_KEY)
    stepper = STEPPERS.get(field)
    draft = await get_draft(state)
    if stepper is None or draft is None:
        await clear_input(state)
        from app.bot.handlers.common import open_menu

        await open_menu(message, state, ctx)
        return
    cancel = keyboards.cancel_only(f"edit:{field}")
    text = (message.text or "").strip()
    if not _NUMBER.match(text):
        await message.answer(texts.stepper_invalid(stepper), reply_markup=cancel)
        return
    value = float(text.replace(",", "."))
    if not stepper.valid(value):
        await message.answer(texts.stepper_invalid(stepper, value), reply_markup=cancel)
        return
    try:
        draft.settings = update_settings(draft.settings, **{field: value})
    except ValueError:
        # Сообщение pydantic пользователю не показываем: там английский текст и служебные детали.
        log.warning("Настройка %s не приняла значение %r", field, value)
        await message.answer(texts.stepper_invalid(stepper, value), reply_markup=cancel)
        return
    await put_draft(state, draft)
    await clear_input(state)
    await render_stepper(message, state, ctx, field)
