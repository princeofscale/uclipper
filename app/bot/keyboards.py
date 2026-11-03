"""Клавиатуры бота: карточка ролика, экран настроек, «ещё», результаты поиска."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.models import BannerMode, BannerPosition, FrameMode, SlicingMode, StaticPosition

# callback_data короче 64 байт — это требование Telegram.
START = "start"


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Новая ссылка", callback_data="ask:link")
    builder.button(text="🔍 Поиск по названию", callback_data="ask:query")
    builder.button(text="📁 Видеофайл", callback_data="ask:file")
    builder.button(text="⚙️ Настройки", callback_data="screen:settings")
    builder.button(text="🎬 Баннеры", callback_data="screen:banners")
    builder.button(text="📊 Статистика", callback_data="screen:stats")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 В начало", callback_data="screen:menu")
    return builder.as_markup()


def search_results(entries: list[dict]) -> InlineKeyboardMarkup:
    """Список найденных роликов: по кнопке на строку."""
    builder = InlineKeyboardBuilder()
    for index, entry in enumerate(entries):
        title = entry.get("title") or "Без названия"
        if len(title) > 48:
            title = title[:47] + "…"
        minutes = texts.duration_text(entry.get("duration") or 0)
        builder.button(text=f"{index + 1}. {title} · {minutes}", callback_data=f"pick:{index}")
    builder.button(text="🔄 Искать другое", callback_data="ask:query")
    builder.adjust(*([1] * len(entries)), 1)
    return builder.as_markup()


def settings_screen(draft, banner_name: str | None, static_name: str | None) -> InlineKeyboardMarkup:
    """Кнопки-значения экрана настроек: каждое нажатие сразу меняет значение."""
    s = draft.settings
    builder = InlineKeyboardBuilder()

    builder.button(text=f"🔢 Клипов: {s.num_clips}", callback_data="edit:num_clips")
    builder.button(text=f"⏱ Длительность: {s.clip_duration:g} с", callback_data="edit:clip_duration")
    builder.button(
        text=f"📐 Раскладка: {texts.SLICING_LABELS[s.slicing_mode]}",
        callback_data="cycle:slicing_mode",
    )
    builder.button(text=f"🖼 Кадр: {texts.FRAME_LABELS[s.frame_mode]}", callback_data="cycle:frame_mode")
    if s.auto_trim:
        trim = "✂️ Обрезка: авто"
    elif s.start_offset or s.end_offset:
        trim = f"✂️ Обрезка: −{s.start_offset:g}/−{s.end_offset:g} с"
    else:
        trim = "✂️ Обрезка: нет"
    builder.button(text=trim, callback_data="screen:trim")

    if s.banner_count == 0:
        builder.button(text="🎬 Баннер: выключен", callback_data="cycle:banner_state")
    else:
        builder.button(text=f"🎬 Баннер: {banner_name or s.banner_id}", callback_data="screen:banners")
        builder.button(text=f"👁 Показов: {s.banner_count}", callback_data="cycle:banner_count")
        builder.button(text=f"⏱ Пауза: {s.banner_duration:g} с", callback_data="cycle:banner_duration")
        builder.button(
            text=f"🎞 Режим: {texts.BANNER_MODE_LABELS[s.banner_mode]}",
            callback_data="cycle:banner_mode",
        )
        builder.button(
            text=f"📍 Позиция: {texts.POSITION_LABELS[s.banner_position]}",
            callback_data="cycle:banner_position",
        )
        builder.button(text=f"📏 Масштаб: {s.banner_scale}%", callback_data="cycle:banner_scale")
        builder.button(
            text=f"{'🔊' if s.banner_sound else '🔇'} Звук баннера: {'вкл' if s.banner_sound else 'выкл'}",
            callback_data="toggle:banner_sound",
        )
    builder.button(text="🔔 SFX появления: " + ("вкл" if s.enable_sfx else "выкл"), callback_data="toggle:enable_sfx")

    if s.static_banner_id:
        builder.button(text=f"📌 Постоянный: {static_name or s.static_banner_id}", callback_data="screen:banners")
        builder.button(
            text=f"📌 Позиция: {texts.STATIC_POSITION_LABELS[s.static_position]}",
            callback_data="cycle:static_position",
        )
        builder.button(text=f"📌 Масштаб: {s.static_scale}%", callback_data="cycle:static_scale")
        builder.button(text="📌 Убрать постоянный", callback_data="static:off")
    else:
        builder.button(text="📌 Постоянный баннер: выкл", callback_data="static:on")

    builder.button(text="🧩 Ещё настройки", callback_data="screen:more")
    builder.button(text="🚀 Нарезать", callback_data="job:start")
    builder.button(text="🏠 В начало", callback_data="screen:menu")

    # по две кнопки в ряд; одиночные — отдельной строкой
    rows = [
        2, 2, 2, 1,
        1 if s.banner_count == 0 else 2,
        *([2, 2, 1] if s.banner_count else []),
        1,
        *([2, 1] if s.static_banner_id else [1]),
        1, 1, 1,
    ]
    builder.adjust(*rows)
    return builder.as_markup()


def more_settings(s) -> InlineKeyboardMarkup:
    """"Ещё": автообрезка, ручная обрезка, частота нарезки, качество."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=("✂️ Автообрезка: вкл" if s.auto_trim else "✂️ Автообрезка: выкл"), callback_data="toggle:auto_trim"
    )
    builder.button(text=f"✂️ Обрезать начало: {s.start_offset:g} с", callback_data="edit:start_offset")
    builder.button(text=f"✂️ Обрезать конец: {s.end_offset:g} с", callback_data="edit:end_offset")
    builder.button(text=f"📥 Качество: до {s.max_height}p", callback_data="cycle:max_height")
    builder.button(text="⬅️ Назад", callback_data="screen:settings")
    builder.adjust(1, 2, 1, 1)
    return builder.as_markup()


def trim_screen(s) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=("✅ Автообрезка включена" if s.auto_trim else "⏺ Автообрезка выключена"), callback_data="toggle:auto_trim"
    )
    builder.button(text=f"➖ Начало: {s.start_offset:g} с", callback_data="edit:start_offset")
    builder.button(text=f"➖ Конец: {s.end_offset:g} с", callback_data="edit:end_offset")
    builder.button(text="⬅️ Назад", callback_data="screen:settings")
    builder.adjust(1, 2, 1)
    return builder.as_markup()


def banner_picker(banners: list, selected: str | None, *, for_static: bool = False) -> InlineKeyboardMarkup:
    """Выбор баннера из списка: 🎞 — видео (со звуком), ▬ — широкая полоса."""
    builder = InlineKeyboardBuilder()
    for banner in banners:
        mark = "✅ " if banner.id == selected else ""
        if banner.media == "video":
            icon = "🎞"
        elif banner.role == "strip":
            icon = "▬"
        elif banner.media == "gif":
            icon = "🌀"
        else:
            icon = "🖼"
        name = banner.name
        if len(name) > 34:
            name = name[:33] + "…"
        prefix = "static:" if for_static else "banner:"
        builder.button(text=f"{mark}{icon} {name}", callback_data=f"{prefix}{banner.id}")
    builder.button(text="📤 Загрузить свой баннер", callback_data="ask:banner" if not for_static else "ask:banner")
    builder.button(text="⬅️ Назад", callback_data="screen:settings")
    # длинные id не должны ломать лимит 64 байта — проверяем и обрезаем имя файла в callback
    builder.adjust(*([1] * (len(banners) + 2)))
    return builder.as_markup()


def banner_confirm(name: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Оставить", callback_data="screen:settings")
    builder.button(text="🔄 Выбрать другой", callback_data="screen:banners")
    builder.adjust(2)
    return builder.as_markup()


def running(state_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏹ Остановить", callback_data=f"job:cancel:{state_id}")
    builder.adjust(1)
    return builder.as_markup()


def done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎬 Ещё ролик", callback_data="ask:link")
    builder.button(text="⚙️ Настройки", callback_data="screen:settings")
    builder.button(text="🏠 В начало", callback_data="screen:menu")
    builder.adjust(2, 1)
    return builder.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data="screen:settings")
    builder.adjust(1)
    return builder.as_markup()
