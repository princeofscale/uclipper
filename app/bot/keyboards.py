"""Клавиатуры бота. Весь интерфейс — inline-кнопки, команд (/…) в боте нет.

Цвета кнопок (Bot API 9.4, поле `style`) используются по смыслу, а не для украшения:
• синий (`primary`)  — главное действие экрана и текущий выбор;
• зелёный (`success`) — «сделать»: запустить нарезку, подтвердить;
• красный (`danger`)  — остановить или убрать.
Остальное остаётся нейтральным — так цветные кнопки действительно бросаются в глаза.

callback_data короче 64 байт — это требование Telegram.
"""
from __future__ import annotations

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.steppers import Stepper

PRIMARY = ButtonStyle.PRIMARY.value
SUCCESS = ButtonStyle.SUCCESS.value
DANGER = ButtonStyle.DANGER.value


def _add(builder: InlineKeyboardBuilder, text: str, data: str, style: str | None = None) -> None:
    builder.button(text=text, callback_data=data, style=style)


def _mark(on: bool) -> str:
    return "✅" if on else "⬜"


# ---------- главное меню и навигация ----------


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "🔗 Прислать ссылку", "ask:link", PRIMARY)
    _add(builder, "🔍 Найти по названию", "ask:query")
    _add(builder, "📁 Загрузить файл", "ask:file")
    _add(builder, "🎬 Баннеры", "screen:banners")
    _add(builder, "⚙️ Настройки", "screen:settings")
    _add(builder, "📊 Статистика", "screen:stats")
    _add(builder, "❓ Справка", "screen:help")
    builder.adjust(1, 2, 2, 2)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "🏠 В начало", "screen:menu")
    return builder.as_markup()


def cancel_only(back: str = "screen:settings") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "✖️ Отмена", back, DANGER)
    return builder.as_markup()


def search_results(entries: list[dict]) -> InlineKeyboardMarkup:
    """Список найденных роликов: по кнопке на строку."""
    builder = InlineKeyboardBuilder()
    for index, entry in enumerate(entries):
        title = entry.get("title") or "Без названия"
        if len(title) > 44:
            title = title[:43] + "…"
        minutes = texts.duration_text(entry.get("duration") or 0)
        _add(builder, f"{index + 1}. {title} · {minutes}", f"pick:{index}", PRIMARY if index == 0 else None)
    _add(builder, "🔄 Искать другое", "ask:query")
    _add(builder, "🏠 В начало", "screen:menu")
    builder.adjust(*([1] * len(entries)), 2)
    return builder.as_markup()


# ---------- экран настроек ----------


def _trim_label(s) -> str:
    if s.auto_trim:
        return "✂️ Обрезка: авто"
    if s.start_offset or s.end_offset:
        return f"✂️ Обрезка: −{s.start_offset:g}/−{s.end_offset:g} с"
    return "✂️ Обрезка: нет"


def settings_screen(draft, banner_name: str | None, static_name: str | None) -> InlineKeyboardMarkup:
    """Экран настроек: каждая кнопка показывает текущее значение; нажатие меняет его или открывает выбор."""
    s = draft.settings
    builder = InlineKeyboardBuilder()
    rows: list[int] = []

    _add(builder, f"🔢 Клипов: {s.num_clips}", "edit:num_clips")
    _add(builder, f"⏱ Длительность: {s.clip_duration:g} с", "edit:clip_duration")
    _add(builder, f"📐 {texts.SLICING_LABELS[s.slicing_mode].capitalize()}", "cycle:slicing_mode")
    _add(builder, f"🖼 {texts.FRAME_LABELS[s.frame_mode]}", "cycle:frame_mode")
    _add(builder, _trim_label(s), "screen:trim")
    rows += [2, 2, 1]

    if s.banner_count == 0:
        _add(builder, "🎬 Баннер: выключен", "cycle:banner_state")
        rows += [1]
    else:
        _add(builder, f"🎬 {banner_name or s.banner_id}", "screen:banners", PRIMARY)
        _add(builder, f"👁 Показов: {s.banner_count}", "cycle:banner_count")
        _add(builder, f"⏱ Пауза: {s.banner_duration:g} с", "cycle:banner_duration")
        _add(builder, f"🎞 {texts.BANNER_MODE_LABELS[s.banner_mode].capitalize()}", "cycle:banner_mode")
        _add(builder, f"📍 {texts.POSITION_LABELS[s.banner_position].capitalize()}", "cycle:banner_position")
        _add(builder, f"📏 Масштаб: {s.banner_scale}%", "cycle:banner_scale")
        _add(builder, f"{'🔊' if s.banner_sound else '🔇'} Звук баннера", "toggle:banner_sound")
        rows += [1, 2, 2, 2]
    _add(builder, f"🔔 Звук появления: {'вкл' if s.enable_sfx else 'выкл'}", "toggle:enable_sfx")
    rows += [1]

    if s.static_banner_id:
        _add(builder, f"📌 {static_name or s.static_banner_id}", "screen:banners", PRIMARY)
        _add(builder, f"📌 {texts.STATIC_POSITION_LABELS[s.static_position].capitalize()}", "cycle:static_position")
        _add(builder, f"📌 Масштаб: {s.static_scale}%", "cycle:static_scale")
        _add(builder, "🗑 Убрать", "static:off", DANGER)
        rows += [1, 2, 1]
    else:
        _add(builder, "📌 Постоянный баннер: выкл", "static:on")
        rows += [1]

    _add(builder, "🧩 Ещё настройки", "screen:more")
    _add(builder, "🚀 Нарезать", "job:start", SUCCESS)
    _add(builder, "🏠 В начало", "screen:menu")
    rows += [1, 1, 1]

    builder.adjust(*rows)
    return builder.as_markup()


def stepper_screen(stepper: Stepper, value: float) -> InlineKeyboardMarkup:
    """Выбор числа: пресеты, «−/+» и ввод своего значения."""
    builder = InlineKeyboardBuilder()
    for preset in stepper.presets:
        selected = preset == value
        label = f"{preset:g}" + (f" {stepper.unit}" if stepper.unit else "")
        _add(builder, f"● {label}" if selected else label, f"set:{stepper.field}:{preset:g}", PRIMARY if selected else None)
    step = f"{stepper.step:g}"
    _add(builder, f"−{step}", f"step:{stepper.field}:-{step}")
    _add(builder, stepper.show(value), "noop")
    _add(builder, f"+{step}", f"step:{stepper.field}:{step}")
    _add(builder, "✍️ Ввести своё число", f"type:{stepper.field}")
    _add(builder, "✅ Готово", stepper.back, SUCCESS)
    columns = 3 if len(stepper.presets) % 3 == 0 else 2
    rows = [columns] * (len(stepper.presets) // columns) + [len(stepper.presets) % columns] * (
        1 if len(stepper.presets) % columns else 0
    )
    builder.adjust(*rows, 3, 1, 1)
    return builder.as_markup()


def more_settings(s) -> InlineKeyboardMarkup:
    """«Ещё»: автообрезка, ручная обрезка, качество скачивания."""
    builder = InlineKeyboardBuilder()
    _add(builder, f"{_mark(s.auto_trim)} Автообрезка", "toggle:auto_trim:more", SUCCESS if s.auto_trim else None)
    _add(builder, f"✂️ Начало: −{s.start_offset:g} с", "edit:start_offset")
    _add(builder, f"✂️ Конец: −{s.end_offset:g} с", "edit:end_offset")
    _add(builder, f"📥 Качество: до {s.max_height}p", "cycle:max_height")
    _add(builder, "⬅️ Назад", "screen:settings")
    builder.adjust(1, 2, 1, 1)
    return builder.as_markup()


def trim_screen(s) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, f"{_mark(s.auto_trim)} Автообрезка", "toggle:auto_trim:trim", SUCCESS if s.auto_trim else None)
    _add(builder, f"➖ Начало: {s.start_offset:g} с", "edit:start_offset")
    _add(builder, f"➖ Конец: {s.end_offset:g} с", "edit:end_offset")
    _add(builder, "⬅️ Назад", "screen:settings")
    builder.adjust(1, 2, 1)
    return builder.as_markup()


# ---------- баннеры ----------


def _banner_icon(banner) -> str:
    if banner.media == "video":
        return "🎞"
    if banner.role == "strip":
        return "▬"
    if banner.media == "gif":
        return "🌀"
    return "🖼"


def banner_picker(banners: list, selected: tuple[str | None, ...] = (), *, with_draft: bool = True) -> InlineKeyboardMarkup:
    """Выбор баннера. 🎞 — видео со звуком, ▬ — полоса под постоянный баннер, 🌀 — анимация."""
    builder = InlineKeyboardBuilder()
    for banner in banners:
        name = banner.name if len(banner.name) <= 32 else banner.name[:31] + "…"
        current = banner.id in selected
        _add(builder, f"{'✅ ' if current else ''}{_banner_icon(banner)} {name}", f"banner:{banner.id}", PRIMARY if current else None)
    _add(builder, "📤 Загрузить свой", "ask:banner", SUCCESS)
    _add(builder, "⬅️ Назад" if with_draft else "🏠 В начало", "screen:settings" if with_draft else "screen:menu")
    builder.adjust(*([1] * (len(banners) + 2)))
    return builder.as_markup()


# ---------- нарезка ----------


def running(job_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "⏹ Остановить", f"job:cancel:{job_id}", DANGER)
    return builder.as_markup()


def done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "🎬 Ещё ролик", "ask:link", PRIMARY)
    _add(builder, "⚙️ Настройки", "screen:settings")
    _add(builder, "🏠 В начало", "screen:menu")
    builder.adjust(1, 2)
    return builder.as_markup()

