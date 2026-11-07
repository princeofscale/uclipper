"""Клавиатуры бота. Весь интерфейс — inline-кнопки, команд (/…) в боте нет.

Цвета кнопок (Bot API 9.4, поле `style`) используются по смыслу, а не для украшения:
• синий (`primary`)  — главное действие экрана и текущий выбор;
• зелёный (`success`) — «сделать»: запустить нарезку, подтвердить;
• красный (`danger`)  — остановить или убрать.
Остальное остаётся нейтральным — так цветные кнопки действительно бросаются в глаза.

Правило подписей: кнопка настройки показывает её название и текущее значение («🖼 Кадр: по центру, размытый фон»),
а нажатие открывает выбор — так видно и что меняется, и на что.

callback_data короче 64 байт — это требование Telegram.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from aiogram.enums import ButtonStyle
from aiogram.types import CopyTextButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.busy import Busy, Cause
from app.bot.options import CHOICES
from app.bot.steppers import Stepper
from app.models import ClipSettings, Draft, Goal
from app.services.autoplan import GOALS
from app.services.banner_manager import BannerInfo
from app.services.history import Record
from app.services.platforms import EVERYWHERE, PLATFORM_BY_ID, SEARCHABLE, TRENDING, Platform
from app.services.trends import CATEGORIES

PRIMARY = ButtonStyle.PRIMARY.value
SUCCESS = ButtonStyle.SUCCESS.value
DANGER = ButtonStyle.DANGER.value

MENU = "screen:menu"
REFRESH = "menu:refresh"  # «Проверить»: узнать, освободился ли бот


Chip = tuple[str, str, str | None]  # (подпись, callback_data, цвет)


def _add(builder: InlineKeyboardBuilder, text: str, data: str, style: str | None = None) -> None:
    builder.button(text=text, callback_data=data, style=style)


def _rows(count: int, per_row: int) -> tuple[int, ...]:
    """Раскладка `count` кнопок рядами по `per_row` (последний ряд может быть короче)."""
    return (per_row,) * (count // per_row) + ((count % per_row,) if count % per_row else ())


def chip_rows(count: int) -> tuple[int, ...]:
    """Ряды кнопок выбора площадки: до трёх в один ряд, больше — по две."""
    return _rows(count, 3 if count <= 3 else 2)


def scope_chips(options: Sequence[Platform], selected: str, make_data: Callable[[str], str]) -> list[Chip]:
    """Кнопки выбора площадки: «Везде» и каждая из `options`. Выбранная отмечена точкой и подсвечена синим."""
    choices = [(EVERYWHERE, "🌐 Везде"), *((platform.id, f"{platform.emoji} {platform.title}") for platform in options)]
    return [
        (f"{'● ' if choice == selected else ''}{label}", make_data(choice), PRIMARY if choice == selected else None)
        for choice, label in choices
    ]


# ---------- главное меню и навигация ----------


def main_menu(busy: Busy | None = None) -> InlineKeyboardMarkup:
    """Главное меню. Пока бот занят (`busy`), четыре кнопки нового ролика красные и с замком: нажатие только объясняет
    причину. Справочные кнопки работают всегда, а сверху появляются «Проверить» и (для своей нарезки) «Остановить»."""
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    if busy is not None:
        if busy.cause is Cause.MINE and busy.job_id:
            _add(builder, "⏹ Остановить", f"job:cancel:{busy.job_id}", DANGER)
        _add(builder, "🔄 Проверить", REFRESH)
        rows.append(2 if busy.cause is Cause.MINE and busy.job_id else 1)

    def source(icon: str, caption: str, data: str, *, main: bool = False) -> None:
        if busy is None:
            _add(builder, f"{icon} {caption}", data, PRIMARY if main else None)
        else:
            _add(builder, f"🔒 {caption}", data, DANGER)

    source("🔗", "Прислать ссылку", "ask:link", main=True)
    source("🔍", "Найти по названию", "ask:query")
    source("📁", "Загрузить файл", "ask:file")
    source("🔥", "В тренде", "screen:trends")
    _add(builder, "🎬 Баннеры", "screen:banners")
    _add(builder, "🕘 История", "screen:stats")
    _add(builder, "❓ Справка", "screen:help")
    builder.adjust(*rows, 1, 2, 2, 2)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "🏠 В начало", MENU)
    return builder.as_markup()


def cancel_only(back: str = "screen:manual") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "✖️ Отмена", back, DANGER)
    return builder.as_markup()


def query_prompt(choice: str = EVERYWHERE) -> InlineKeyboardMarkup:
    """Экран «Что искать?»: сверху выбор площадки (выбранная — синяя), снизу путь в начало."""
    builder = InlineKeyboardBuilder()
    chips = scope_chips(SEARCHABLE, choice, lambda option: f"qp:{option}")
    for chip in chips:
        _add(builder, *chip)
    _add(builder, "🏠 В начало", MENU)
    builder.adjust(*chip_rows(len(chips)), 1)
    return builder.as_markup()


def _result_list(
    entries: list[dict], *, chips: Sequence[Chip], again: tuple[str, str], back: tuple[str, str] = ("🏠 В начало", MENU)
) -> InlineKeyboardMarkup:
    """Список найденных роликов: по кнопке на строку, ниже выбор площадки и два пути назад.

    Если в списке несколько площадок, у кнопки ролика стоит значок её площадки.
    """
    builder = InlineKeyboardBuilder()
    mixed = len({entry.get("platform") for entry in entries}) > 1
    for index, entry in enumerate(entries):
        platform = PLATFORM_BY_ID.get(entry.get("platform") or "")
        badge = f"{platform.emoji} " if mixed and platform else ""
        title = entry.get("title") or "Без названия"
        if len(title) > 36:
            title = title[:35] + "…"
        minutes = texts.duration_text(entry.get("duration") or 0)
        _add(builder, f"{index + 1}. {badge}{title} · {minutes}", f"pick:{index}", PRIMARY if index == 0 else None)
    for chip in chips:
        _add(builder, *chip)
    _add(builder, *again)
    _add(builder, *back)
    builder.adjust(*([1] * len(entries)), *chip_rows(len(chips)), 2)
    return builder.as_markup()


def search_results(entries: list[dict], choice: str = EVERYWHERE) -> InlineKeyboardMarkup:
    """Итог поиска по названию (и после неудачи — тогда `entries` пуст): повторить тот же запрос на другой площадке."""
    chips = scope_chips(SEARCHABLE, choice, lambda option: f"qs:{option}")
    return _result_list(entries, chips=chips, again=("🔄 Искать другое", f"qp:{choice}"))


# ---------- режим нарезки ----------


def mode_picker() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "🤖 Авто — подберу сам", "mode:auto", PRIMARY)
    _add(builder, "🛠 Вручную — настрою сам", "mode:manual")
    _add(builder, "🏠 В начало", MENU)
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def auto_screen(goal: Goal, clips: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for candidate, spec in GOALS.items():
        selected = candidate is goal
        name = {"tiktok": "TikTok", "shorts": "Shorts", "universal": "Универсально"}[candidate.value]
        _add(builder, f"{'● ' if selected else ''}{spec.emoji} {name}", f"goal:{candidate.value}", PRIMARY if selected else None)
    _add(builder, f"🚀 Нарезать {clips} {_clips_word(clips)}", "job:start", SUCCESS)
    _add(builder, "🛠 Подправить вручную", "mode:manual")
    _add(builder, "⬅️ Назад", "screen:mode")
    builder.adjust(2, 1, 1, 1, 1)
    return builder.as_markup()


def _clips_word(count: int) -> str:
    """1 клип, 2–4 клипа, 5+ клипов."""
    if count % 100 in range(11, 15):
        return "клипов"
    return {1: "клип", 2: "клипа", 3: "клипа", 4: "клипа"}.get(count % 10, "клипов")


# ---------- ручная настройка ----------


def _trim_caption(s: ClipSettings) -> str:
    if s.auto_trim:
        return "✂️ Края ролика: авто"
    if s.start_offset or s.end_offset:
        return f"✂️ Края ролика: −{s.start_offset:g} / −{s.end_offset:g} с"
    return "✂️ Края ролика: целиком"


def manual_screen(draft: Draft, *, banner_available: bool, banner_name: str | None = None) -> InlineKeyboardMarkup:
    """Ручная настройка: каждая кнопка называет настройку и показывает её значение; нажатие открывает выбор."""
    s = draft.settings
    builder = InlineKeyboardBuilder()
    rows: list[int] = []

    _add(builder, f"🔢 Клипов: {s.num_clips}", "edit:num_clips")
    _add(builder, f"⏱ Длина: {s.clip_duration:g} с", "edit:clip_duration")
    _add(builder, CHOICES["slicing_mode"].caption(s.slicing_mode), "choose:slicing_mode")
    _add(builder, CHOICES["frame_mode"].caption(s.frame_mode), "choose:frame_mode")
    _add(builder, _trim_caption(s), "screen:trim")
    rows += [2, 1, 1, 1]

    if banner_available:
        enabled = s.banner_count > 0
        title = f"Баннер {banner_name}" if banner_name else "Баннер"
        _add(builder, f"{'✅' if enabled else '⬜'} {title}: {'вкл' if enabled else 'выкл'}", "toggle:banner", SUCCESS if enabled else None)
        rows.append(1)
        if enabled:
            _add(builder, f"🔁 Показов: {s.banner_count}", "edit:banner_count")
            _add(builder, f"{'🔊' if s.banner_sound else '🔇'} Звук: {'вкл' if s.banner_sound else 'выкл'}", "toggle:banner_sound")
            _add(builder, CHOICES["banner_mode"].caption(s.banner_mode), "choose:banner_mode")
            _add(builder, CHOICES["banner_position"].caption(s.banner_position), "choose:banner_position")
            _add(builder, f"📏 Размер: {s.banner_scale}%", "edit:banner_scale")
            rows += [2, 2, 1]

    _add(builder, "⚙️ Дополнительно", "screen:more")
    _add(builder, "🚀 Начать нарезку", "job:start", SUCCESS)
    _add(builder, "⬅️ Назад", "screen:mode")
    rows += [1, 1, 1]
    builder.adjust(*rows)
    return builder.as_markup()


def choice_screen(field: str, current: object) -> InlineKeyboardMarkup:
    """Выбор из вариантов: текущий отмечен ✅ и выделен цветом."""
    choice = CHOICES[field]
    selected = choice.option(current).value
    builder = InlineKeyboardBuilder()
    for option in choice.options:
        mine = option.value == selected
        _add(builder, f"{'✅' if mine else option.icon} {option.label}", f"opt:{field}:{option.value}", PRIMARY if mine else None)
    _add(builder, "⬅️ Назад", choice.back)
    builder.adjust(*([1] * (len(choice.options) + 1)))
    return builder.as_markup()


def stepper_screen(stepper: Stepper, value: float) -> InlineKeyboardMarkup:
    """Выбор числа: пресеты, «−/+» и ввод своего значения."""
    builder = InlineKeyboardBuilder()
    for preset in stepper.presets:
        selected = preset == value
        label = stepper.show(preset)
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


def more_settings(s: ClipSettings) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, CHOICES["max_height"].caption(s.max_height), "choose:max_height")
    _add(builder, f"🔔 Сигнал перед баннером: {'вкл' if s.enable_sfx else 'выкл'}", "toggle:enable_sfx:more", SUCCESS if s.enable_sfx else None)
    _add(builder, "⬅️ Назад", "screen:manual")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def trim_screen(s: ClipSettings) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(
        builder,
        f"{'✅' if s.auto_trim else '⬜'} Убрать заставку и титры (авто)",
        "toggle:auto_trim:trim",
        SUCCESS if s.auto_trim else None,
    )
    _add(builder, f"⏩ Пропустить начало: {s.start_offset:g} с", "edit:start_offset")
    _add(builder, f"⏪ Пропустить конец: {s.end_offset:g} с", "edit:end_offset")
    _add(builder, "⬅️ Назад", "screen:manual")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


# ---------- баннеры и тренды ----------


def banners_screen(banners: Sequence[BannerInfo], selected_id: str | None) -> InlineKeyboardMarkup:
    """Баннеры проекта: выбранный отмечен ✅ и подсвечен, «▶️» присылает сам ролик."""
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    for banner in banners:
        chosen = banner.id == selected_id
        _add(
            builder,
            f"{'✅' if chosen else '⬜'} {texts.banner_label(banner)}",
            f"banner:pick:{banner.id}",
            PRIMARY if chosen else None,
        )
        rows.append(1)
    for banner in banners:
        _add(builder, f"▶️ {banner.name}", f"banner:preview:{banner.id}")
        rows.append(len(banners))
    _add(builder, "⬅️ Назад", MENU)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def trends_home(choice: str = EVERYWHERE) -> InlineKeyboardMarkup:
    """Сверху выбор площадки (выбранная — синяя), ниже жанры."""
    builder = InlineKeyboardBuilder()
    chips = scope_chips(TRENDING, choice, lambda option: f"trends:{option}")
    for chip in chips:
        _add(builder, *chip)
    for category in CATEGORIES:
        _add(builder, f"{category.emoji} {category.title}", f"trend:{choice}:{category.id}")
    _add(builder, "🏠 В начало", MENU)
    builder.adjust(*chip_rows(len(chips)), *_rows(len(CATEGORIES), 2), 1)
    return builder.as_markup()


def trend_results(videos: list[dict], choice: str, category_id: str) -> InlineKeyboardMarkup:
    """Ролики жанра: выбрать один, сменить площадку (тот же жанр) или вернуться к жанрам."""
    chips = scope_chips(TRENDING, choice, lambda option: f"trend:{option}:{category_id}")
    return _result_list(videos, chips=chips, again=("⬅️ К жанрам", f"trends:{choice}"))


def trends_retry(choice: str, category_id: str) -> InlineKeyboardMarkup:
    """Не получилось загрузить тренды: повторить или вернуться."""
    builder = InlineKeyboardBuilder()
    _add(builder, "🔄 Повторить", f"trend:{choice}:{category_id}", PRIMARY)
    _add(builder, "⬅️ К жанрам", f"trends:{choice}")
    builder.adjust(1, 1)
    return builder.as_markup()


def history_screen(records: Sequence[Record]) -> InlineKeyboardMarkup:
    """Оценка последних нарезок: по строке 👍/👎 на запись (callback_data укладывается в 64 байта)."""
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    for record in records:
        mark = "✅" if record.rating == "up" else ("❌" if record.rating == "down" else "🎞")
        _add(builder, f"{mark} 👍", f"rate:up:{record.id}")
        _add(builder, "👎", f"rate:down:{record.id}")
        rows.append(2)
    _add(builder, "🏠 В начало", MENU)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


# ---------- нарезка ----------


def running(job_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _add(builder, "⏹ Остановить", f"job:cancel:{job_id}", DANGER)
    return builder.as_markup()


def done(description: str | None = None) -> InlineKeyboardMarkup:
    """Итог нарезки. `description` — готовый текст для кнопки «скопировать»: подпись к публикации.

    Кнопка копирования (`CopyTextButton`) есть в Bot API 10.3, а у нас aiogram 3.31 под этот API — то есть
    ничего обновлять не надо. Без текста кнопки нет: пустая подпись только путает.
    """
    builder = InlineKeyboardBuilder()
    _add(builder, "🎬 Ещё ролик", "ask:link", PRIMARY)
    if description:
        builder.button(text="📋 Скопировать описание", copy_text=CopyTextButton(text=description[:256]))
    _add(builder, "🔁 Нарезать иначе", "screen:mode")
    _add(builder, "🔥 В тренде", "screen:trends")
    _add(builder, "🏠 В начало", MENU)
    if description:
        builder.adjust(1, 1, 2, 1)
    else:
        builder.adjust(1, 2, 1)
    return builder.as_markup()
