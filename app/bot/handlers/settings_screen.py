"""Экран настроек: кнопки-значения, «Ещё» и шаг ввода чисел."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.context import AppContext, edit_or_send, get_draft, put_draft, safe_answer, update_settings
from app.bot.states import Flow
from app.models import BannerMode, BannerPosition, FrameMode, SlicingMode, StaticPosition

log = logging.getLogger(__name__)
router = Router(name="settings")

NUMBER_STEPS: dict[str, tuple[str, str, float, float, float]] = {
    # поле: (заголовок, подсказка, мин, макс, шаг)
    "clip_duration": ("Длительность клипа", "Сколько секунд длится один клип (без пауз под баннер), от 2 до 180.", 2, 180, 5),
    "start_offset": ("Обрезать начало", "Сколько секунд убрать в начале ролика, от 0 до 3600.", 0, 3600, 5),
    "end_offset": ("Обрезать конец", "Сколько секунд убрать в конце ролика, от 0 до 3600.", 0, 3600, 5),
}

MIN_NUM_CLIPS, MAX_NUM_CLIPS = 1, 50
CLIP_COUNT_STEPS = (1, 2, 3, 5, 8, 10, 15, 20, 30, 50)
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


# ---------- отрисовка ----------


async def render_settings(event: Message | CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await edit_or_send(event, texts.START, keyboards.main_menu())
        return
    await state.set_state(Flow.editing)
    banner_name = ctx.banner_name(draft.settings.banner_id)
    static_name = ctx.banner_name(draft.settings.static_banner_id)
    await edit_or_send(
        event,
        texts.settings_screen(draft, banner_name, static_name),
        keyboards.settings_screen(draft, banner_name, static_name),
    )


async def render_more(event: CallbackQuery | Message, state: FSMContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await edit_or_send(event, texts.START, keyboards.main_menu())
        return
    text = (
        f"<b>Ещё настройки</b>\n\n"
        f"✂️ Автообрезка: {'включена — бот сам найдёт, где начинается и кончается ролик' if draft.settings.auto_trim else 'выключена'}\n"
        f"📥 Качество скачивания: до {draft.settings.max_height}p "
        f"(чем выше, тем дольше скачивание и тяжелее клипы)\n\n"
        f"<i>Автообрезка убирает титры, логотипы и эфирное время в начале и конце. "
        f"Ручные смещения при включённой автообрезке не используются.</i>"
    )
    await edit_or_send(event, text, keyboards.more_settings(draft.settings))


# ---------- вход на экран ----------


@router.callback_query(F.data == "screen:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "screen:more")
async def cb_more(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await render_more(callback, state)


# ---------- циклические значения ----------


async def _cycle(callback: CallbackQuery, state: FSMContext, ctx: AppContext, **changes) -> None:
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    draft.settings = update_settings(draft.settings, **changes)
    await put_draft(state, draft)
    await safe_answer(callback)
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "cycle:slicing_mode")
async def cb_cycle_slicing(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    values = list(SlicingMode)
    mode = values[(values.index(draft.settings.slicing_mode) + 1) % len(values)]
    hint = {
        SlicingMode.EVENLY_SPACED: "равномерно по всему ролику",
        SlicingMode.SEQUENTIAL: "подряд с самого начала",
    }[mode]
    await safe_answer(callback, f"Раскладка: {hint}")
    await _cycle(callback, state, ctx, slicing_mode=mode)


@router.callback_query(F.data == "cycle:frame_mode")
async def cb_cycle_frame(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    values = list(FrameMode)
    mode = values[(values.index(draft.settings.frame_mode) + 1) % len(values)]
    hint = {
        FrameMode.BLUR_PAD: "вертикальный кадр на размытом фоне",
        FrameMode.CENTER_CROP: "вертикальный кроп по центру",
        FrameMode.ORIGINAL: "горизонтальный 16:9 без обрезки",
    }[mode]
    await safe_answer(callback, f"Кадр: {hint}")
    await _cycle(callback, state, ctx, frame_mode=mode)


@router.callback_query(F.data == "cycle:banner_count")
async def cb_cycle_banner_count(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    value = next_value(BANNER_COUNT_STEPS, draft.settings.banner_count)
    await safe_answer(callback, "Баннер выключен" if value == 0 else f"Показов на клип: {value}")
    await _cycle(callback, state, ctx, banner_count=value)


@router.callback_query(F.data == "cycle:banner_state")
async def cb_cycle_banner_state(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Баннер выключен → включаем с 1 показом на клип."""
    await safe_answer(callback, "Баннер включён")
    await _cycle(callback, state, ctx, banner_count=1)


@router.callback_query(F.data == "cycle:banner_duration")
async def cb_cycle_banner_duration(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    value = next_value(BANNER_DURATION_STEPS, draft.settings.banner_duration)
    await safe_answer(callback, f"Пауза под баннер: {value:g} с")
    await _cycle(callback, state, ctx, banner_duration=value)


@router.callback_query(F.data == "cycle:banner_mode")
async def cb_cycle_banner_mode(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    values = list(BannerMode)
    mode = values[(values.index(draft.settings.banner_mode) + 1) % len(values)]
    hint = "видео замирает, поверх баннер" if mode is BannerMode.FREEZE else "баннер поверх идущего видео"
    await safe_answer(callback, hint)
    await _cycle(callback, state, ctx, banner_mode=mode)


@router.callback_query(F.data == "cycle:banner_position")
async def cb_cycle_banner_position(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    values = list(BannerPosition)
    position = values[(values.index(draft.settings.banner_position) + 1) % len(values)]
    await safe_answer(callback, f"Позиция: {texts.POSITION_LABELS[position]}")
    await _cycle(callback, state, ctx, banner_position=position)


@router.callback_query(F.data == "cycle:banner_scale")
async def cb_cycle_banner_scale(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    value = next_value(BANNER_SCALE_STEPS, draft.settings.banner_scale)
    await safe_answer(callback, f"Масштаб баннера: {value}% ширины кадра")
    await _cycle(callback, state, ctx, banner_scale=value)


@router.callback_query(F.data == "cycle:static_position")
async def cb_cycle_static_position(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    values = list(StaticPosition)
    position = values[(values.index(draft.settings.static_position) + 1) % len(values)]
    await safe_answer(callback, f"Постоянный баннер: {texts.STATIC_POSITION_LABELS[position]}")
    await _cycle(callback, state, ctx, static_position=position)


@router.callback_query(F.data == "cycle:static_scale")
async def cb_cycle_static_scale(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        return await _cycle(callback, state, ctx)
    value = next_value(STATIC_SCALE_STEPS, draft.settings.static_scale)
    await safe_answer(callback, f"Масштаб полосы: {value}%")
    await _cycle(callback, state, ctx, static_scale=value)


@router.callback_query(F.data == "cycle:max_height")
async def cb_cycle_max_height(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    draft = await get_draft(state)
    if draft is None:
        return
    value = next_value(MAX_HEIGHT_STEPS, draft.settings.max_height)
    draft.settings = update_settings(draft.settings, max_height=value)
    await put_draft(state, draft)
    await render_more(callback, state)


# ---------- переключатели ----------


@router.callback_query(F.data == "toggle:auto_trim")
async def cb_toggle_auto_trim(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    draft.settings = update_settings(draft.settings, auto_trim=not draft.settings.auto_trim)
    await put_draft(state, draft)
    await safe_answer(callback, "Автообрезка включена" if draft.settings.auto_trim else "Автообрезка выключена")
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "toggle:banner_sound")
async def cb_toggle_banner_sound(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    draft.settings = update_settings(draft.settings, banner_sound=not draft.settings.banner_sound)
    await put_draft(state, draft)
    await safe_answer(callback, "Звук баннера включён" if draft.settings.banner_sound else "Звук баннера выключен")
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "toggle:enable_sfx")
async def cb_toggle_sfx(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    draft.settings = update_settings(draft.settings, enable_sfx=not draft.settings.enable_sfx)
    await put_draft(state, draft)
    await safe_answer(callback, "Звук появления включён" if draft.settings.enable_sfx else "Звук появления выключен")
    await render_settings(callback, state, ctx)


# ---------- ввод чисел/количества ----------

# Экран ввода: подсказка + какой ключ ждём
INPUT_KEY = "input_field"


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit_number(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    field = (callback.data or "edit:").split(":", 1)[1]
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    await safe_answer(callback)
    if field == "clip_duration":
        await state.set_state(Flow.editing)
        await state.update_data({INPUT_KEY: field})
        current = draft.settings.clip_duration
        await edit_or_send(
            callback,
            f"<b>Длительность клипа</b>\nСейчас: {current:g} с.\n\n"
            f"Напишите число секунд от 2 до 180 — например, 30.\n"
            f"<i>Это чистая длительность без пауз под баннер.</i>",
            keyboards.cancel_only(),
        )
        return
    if field == "num_clips":
        await state.update_data({INPUT_KEY: field})
        await edit_or_send(
            callback,
            f"<b>Сколько клипов нарезать?</b>\nСейчас: {draft.settings.num_clips}.\n\nНапишите число от 1 до 50.",
            keyboards.cancel_only(),
        )
        return
    if field in NUMBER_STEPS:
        title, hint, low, high, _ = NUMBER_STEPS[field]
        await state.update_data({INPUT_KEY: field})
        await edit_or_send(
            callback,
            f"<b>{title}</b>\n{hint}\n\nНапишите число от {low:g} до {high:g}.",
            keyboards.cancel_only(),
        )
        return
    await safe_answer(callback, "Эту настройку пока нельзя ввести числом", alert=True)


@router.message(Flow.editing, F.text.regexp(r"^\s*\d+([.,]\d+)?\s*$"))
async def on_number(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Принимает число для настройки, выбранной кнопкой."""
    data = await state.get_data()
    field = data.get(INPUT_KEY)
    if not field:
        return  # пользователь просто прислал число — не наш случай
    draft = await get_draft(state)
    if draft is None:
        await message.answer(texts.START, reply_markup=keyboards.main_menu())
        return
    value = float((message.text or "0").strip().replace(",", "."))
    errors = _validate(field, value)
    if errors:
        await message.answer(errors)
        return
    try:
        draft.settings = update_settings(draft.settings, **{field: value})
    except ValueError:
        # Сообщение pydantic пользователю не показываем: там английский текст и служебные детали.
        log.warning("Настройка %s не приняла значение %r", field, value)
        await message.answer("Это значение не подходит. Напишите другое число.")
        return
    await put_draft(state, draft)
    await state.update_data({INPUT_KEY: None})
    await render_settings(message, state, ctx)


def _validate(field: str, value: float) -> str | None:
    if field == "num_clips":
        if value != int(value):
            return f"Клипов может быть только целое число — от {MIN_NUM_CLIPS} до {MAX_NUM_CLIPS}."
        if not (MIN_NUM_CLIPS <= value <= MAX_NUM_CLIPS):
            return f"Клипов может быть от {MIN_NUM_CLIPS} до {MAX_NUM_CLIPS}. Напишите другое число."
    if field == "clip_duration" and not (2 <= value <= 180):
        return "Длительность клипа — от 2 до 180 секунд. Напишите другое число."
    if field in ("start_offset", "end_offset") and not (0 <= value <= 3600):
        return "Смещение — от 0 до 3600 секунд. Напишите другое число."
    return None


@router.callback_query(F.data == "num:minus")
async def cb_num_minus(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await _shift_num(callback, state, ctx, -1)


@router.callback_query(F.data == "num:plus")
async def cb_num_plus(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await _shift_num(callback, state, ctx, +1)


async def _shift_num(callback: CallbackQuery, state: FSMContext, ctx: AppContext, delta: int) -> None:
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    value = max(MIN_NUM_CLIPS, min(MAX_NUM_CLIPS, draft.settings.num_clips + delta))
    draft.settings = update_settings(draft.settings, num_clips=value)
    await put_draft(state, draft)
    await safe_answer(callback, f"Клипов: {value}")
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "static:on")
async def cb_static_on(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Включает постоянный баннер полосой из пресетов (первая найденная полоса)."""
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
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
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    draft.settings = update_settings(draft.settings, static_banner_id=None)
    await put_draft(state, draft)
    await safe_answer(callback, "Постоянный баннер убран")
    await render_settings(callback, state, ctx)


@router.callback_query(F.data == "screen:trim")
async def cb_trim(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, "Черновик потерялся — начните заново", alert=True)
        return
    await safe_answer(callback)
    s = draft.settings
    text = (
        "<b>Обрезка ролика</b>\n\n"
        f"Автообрезка: {'включена' if s.auto_trim else 'выключена'}\n"
        f"Начало: −{s.start_offset:g} с · Конец: −{s.end_offset:g} с\n\n"
        "<i>Автообрезка сама убирает заставку, титры и пустые края. "
        "Если она включена, ручные смещения не используются. "
        "Концы можно оставить и как есть — тогда бот возьмёт ролик целиком.</i>"
    )
    await edit_or_send(callback, text, keyboards.trim_screen(s))
