"""Тексты бота. Всё по-русски и в одном месте, чтобы их было легко править.

Разметка — HTML Telegram: `<b>`, `<i>`, `<code>`, `<blockquote>` и сворачиваемая
`<blockquote expandable>` для длинных второстепенных кусков. Команд (/…) в боте нет — только кнопки,
поэтому ни один текст на них не ссылается.
"""
from __future__ import annotations

from app.bot.steppers import Stepper
from app.models import BannerMode, BannerPosition, FrameMode, JobState, JobStatus, SlicingMode, StaticPosition

# ---------- главное меню и справка ----------

START = """\
🎬 <b>ClipBanner</b>

Нарежу YouTube-ролик на вертикальные клипы 9:16 и вклею в них рекламный баннер.

<blockquote>Просто пришлите <b>ссылку</b>, <b>название</b> ролика или <b>видеофайл</b> — дальше всё кнопками.</blockquote>"""

HELP = """\
❓ <b>Как это работает</b>

<b>1.</b> Пришлите ссылку на YouTube, название ролика или видеофайл.
<b>2.</b> Настройте клипы и баннер кнопками под карточкой ролика.
<b>3.</b> Нажмите «🚀 Нарезать» — готовые клипы придут в чат по одному, а если они влезают в 50 МБ, то ещё и одним ZIP.

<blockquote expandable><b>Баннер</b>
• <b>всплывающий</b> — кадр и звук замирают, поверх играет рекламный ролик, потом видео продолжается с того же места;
• <b>постоянный</b> — полоса в поле кадра на весь клип.
Свой баннер можно загрузить прямо в чат: картинка, GIF или видео до 20 секунд.

<b>Обрезка</b>
Автообрезка убирает заставку, титры и пустые края по звуку и яркости (это эвристика, не нейросеть). Её можно заменить ручными числами.

<b>Ограничения Telegram</b>
• бот принимает файл до 20 МБ — для ролика побольше пришлите ссылку;
• бот отдаёт файл до 50 МБ — клип потяжелее он перекодирует в 720×1280.</blockquote>"""

REQUEST_LINK = "🔗 Пришлите ссылку на YouTube-ролик.\n<i>Например: https://youtu.be/dQw4w9WgXcQ</i>"

REQUEST_QUERY = (
    "🔍 Напишите название ролика — найду на YouTube до 5 вариантов.\n"
    "<i>Можно уточнить: «дуров интервью лекс фридман»</i>"
)

REQUEST_FILE = (
    "📁 Пришлите видеофайл (mp4, mov или mkv) — до 20 МБ.\n"
    "<i>Если ролик больше, проще прислать ссылку на YouTube.</i>"
)

REQUEST_BANNER = (
    "📤 Пришлите баннер: картинку (png, jpg, webp, gif) или видео (mp4, mov, webm) до 20 секунд."
)

NUDGE = "👇 Выберите действие кнопкой — или просто пришлите ссылку, название или видеофайл."
NOT_ALLOWED = "🔒 Это закрытый бот.\nВаш ID: <code>{user_id}</code> — передайте его владельцу, чтобы он добавил вас."

SEARCHING = "🔍 Ищу на YouTube: <b>{query}</b>…"
SEARCH_NOTHING = "Ничего не нашлось по запросу «{query}».\nПопробуйте другое название или пришлите ссылку на YouTube."
FETCHING = "⏳ Смотрю ролик…"

BAD_LINK = (
    "Это не похоже на ссылку на YouTube-ролик.\n\n"
    "Подойдут: https://www.youtube.com/watch?v=…, https://youtu.be/…, …/shorts/…\n"
    "Либо пришлите название ролика — поищу."
)

DOWNLOADING_FILE = "⏳ Скачиваю видеофайл…"
FILE_TOO_BIG = "Файл больше 20 МБ — обычный бот не примет его. Пришлите ссылку на YouTube: качество выберем сами."
FILE_BAD = "Не получилось прочитать видеофайл: {error}\nПришлите другой файл или ссылку на YouTube."
NOT_A_VIDEO = "Это не видеофайл. Подойдут .mp4, .mov и .mkv — или пришлите ссылку на YouTube."

STALE = "Эта кнопка устарела — бот перезапускался. Начнём заново 👇"
LOST_DRAFT = "Ролик потерялся — пришлите его заново"
NEED_VIDEO = "Сначала пришлите ролик — настройки применяются к нему"


def duration_text(seconds: float) -> str:
    """`95` → «1:35», `3725` → «1:02:05»."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _escape(text: str) -> str:
    """Экранирует HTML-спецсимволы: названия роликов часто содержат `<`, `&` и кавычки."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- поиск ----------


def search_cards(entries: list[dict]) -> str:
    lines = ["🔍 <b>Нашёл такие ролики</b> — выберите нужный:"]
    for index, entry in enumerate(entries, start=1):
        title = _escape(entry.get("title") or "Без названия")
        uploader = _escape(entry.get("uploader") or "")
        minutes = duration_text(entry.get("duration") or 0)
        meta = " · ".join(part for part in (uploader, minutes) if part)
        lines.append(f"\n<b>{index}.</b> {title}\n<i>{meta}</i>")
    return "\n".join(lines)


# ---------- карточка ролика и настройки ----------


def video_card(title: str, uploader: str | None, duration: float) -> str:
    """Шапка экрана настроек: название и автор с длиной."""
    lines = [f"<b>{_escape(title)}</b>"]
    meta = [part for part in (_escape(uploader or ""), duration_text(duration) if duration else "") if part]
    if meta:
        lines.append(f"<i>{' · '.join(meta)}</i>")
    return "\n".join(lines)


FRAME_LABELS = {
    FrameMode.BLUR_PAD: "9:16 размытый фон",
    FrameMode.CENTER_CROP: "9:16 кроп",
    FrameMode.ORIGINAL: "16:9 как есть",
}
SLICING_LABELS = {SlicingMode.EVENLY_SPACED: "равномерно", SlicingMode.SEQUENTIAL: "подряд"}
BANNER_MODE_LABELS = {BannerMode.FREEZE: "пауза", BannerMode.OVERLAY: "поверх"}
POSITION_LABELS = {BannerPosition.CENTER: "центр", BannerPosition.TOP: "верх", BannerPosition.BOTTOM: "низ"}
STATIC_POSITION_LABELS = {StaticPosition.TOP: "верхняя", StaticPosition.BOTTOM: "нижняя"}


def trim_text(s) -> str:
    if s.auto_trim:
        return "авто"
    parts = []
    if s.start_offset:
        parts.append(f"начало −{s.start_offset:g} с")
    if s.end_offset:
        parts.append(f"конец −{s.end_offset:g} с")
    return ", ".join(parts) or "нет"


def settings_screen(draft, banner_name: str | None, static_name: str | None) -> str:
    """Экран настроек: карточка ролика и два блока — нарезка и баннеры."""
    s = draft.settings
    cut = (
        f"✂️ <b>Нарезка</b>\n"
        f"{s.num_clips} × {s.clip_duration:g} с · {SLICING_LABELS[s.slicing_mode]}\n"
        f"Кадр: {FRAME_LABELS[s.frame_mode]}\n"
        f"Обрезка: {trim_text(s)}"
    )
    if s.banner_count > 0:
        popup = (
            f"{_escape(banner_name or s.banner_id or '—')}\n"
            f"{s.banner_count} на клип · {BANNER_MODE_LABELS[s.banner_mode]} · "
            f"{POSITION_LABELS[s.banner_position]} · {s.banner_scale}% · пауза {s.banner_duration:g} с\n"
            f"Звук баннера: {'вкл' if s.banner_sound else 'выкл'}"
        )
    else:
        popup = "всплывающий: выключен"
    if s.static_banner_id:
        static = (
            f"📌 {_escape(static_name or s.static_banner_id)} · "
            f"{STATIC_POSITION_LABELS[s.static_position]} · {s.static_scale}%"
        )
    else:
        static = "📌 постоянный: выключен"
    banners = f"🎬 <b>Баннер</b>\n{popup}\n{static}"
    return (
        f"{video_card(draft.title, draft.uploader, draft.duration)}\n\n"
        f"<blockquote>{cut}</blockquote>\n"
        f"<blockquote>{banners}</blockquote>\n"
        f"<i>Качество скачивания: до {s.max_height}p</i>"
    )


def stepper_screen(stepper: Stepper, value: float) -> str:
    return (
        f"<b>{stepper.title}</b>\n"
        f"{stepper.hint}\n\n"
        f"Сейчас: <b>{stepper.show(value)}</b> <i>({stepper.range_text()})</i>"
    )


def stepper_prompt(stepper: Stepper, value: float) -> str:
    return (
        f"✍️ <b>{stepper.title}</b>\n"
        f"Сейчас: {stepper.show(value)}.\n\n"
        f"Напишите число {stepper.range_text()}" + (" — целое." if stepper.integer else ".")
    )


def stepper_invalid(stepper: Stepper, value: float | None = None) -> str:
    """Подсказка, когда ввод не подошёл. `None` — это вообще не число."""
    if value is not None and stepper.integer and value != int(value):
        return f"Здесь нужно целое число {stepper.range_text()}."
    return f"Нужно число {stepper.range_text()}. Напишите другое."


def more_screen(s) -> str:
    return (
        "<b>🧩 Ещё настройки</b>\n\n"
        f"✂️ Автообрезка: <b>{'включена' if s.auto_trim else 'выключена'}</b>\n"
        f"📥 Качество скачивания: <b>до {s.max_height}p</b>\n\n"
        "<blockquote>Автообрезка сама убирает заставку, титры и пустые края. "
        "Когда она включена, ручные смещения не используются. "
        "Чем выше качество, тем дольше скачивание и тяжелее клипы.</blockquote>"
    )


def trim_screen(s) -> str:
    return (
        "<b>✂️ Обрезка ролика</b>\n\n"
        f"Автообрезка: <b>{'включена' if s.auto_trim else 'выключена'}</b>\n"
        f"Начало: −{s.start_offset:g} с · Конец: −{s.end_offset:g} с\n\n"
        "<blockquote>Автообрезка сама убирает заставку, титры и пустые края. "
        "Если она включена, ручные смещения не используются. "
        "Без обрезки бот берёт ролик целиком.</blockquote>"
    )


# ---------- прогресс и результат ----------

BAR_WIDTH = 20


def progress_bar(percent: float) -> str:
    filled = max(0, min(BAR_WIDTH, round(percent / 100 * BAR_WIDTH)))
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


QUEUED_TEXT = "⏳ <b>Задача в очереди</b>"


def progress_text(state: JobState) -> str:
    """Сообщение-прогресс: стадия, полоса, счётчик клипов и (свёрнуто) последние строки лога."""
    head = {
        JobStatus.QUEUED: "⏳ Задача в очереди",
        JobStatus.RUNNING: f"⚙️ {_escape(state.stage)}",
        JobStatus.DONE: "✅ Готово",
        JobStatus.ERROR: "❌ Ошибка",
        JobStatus.CANCELLED: "⏹ Отменено",
    }[state.status]
    lines = [f"<b>{head}</b>", f"<code>{progress_bar(state.progress)}</code> {state.progress:.0f}%"]
    if state.clips:
        lines.append(f"Роликов готово: {len(state.clips)}")
    trim = state.trim
    if trim is not None and (trim.head or trim.tail):
        lines.append(f"<i>Обрезал: {' · '.join(part for part in (trim.head, trim.tail) if part)}</i>")
    tail = [line for line in state.log[-3:] if line]
    if tail and state.status is JobStatus.RUNNING:
        body = "\n".join(_escape(line) for line in tail)
        lines.append(f"<blockquote expandable>{body}</blockquote>")
    return "\n".join(lines)


def _source_range(state: JobState) -> str:
    trim = state.trim
    if trim is None:
        return "весь"
    return f"с {duration_text(trim.start)} по {duration_text(trim.end)}"


def results_summary(state: JobState) -> str:
    """Итог задачи: сколько роликов, длительность, что с архивом."""
    total = sum(clip.duration for clip in state.clips)
    lines = [
        "✅ <b>Готово!</b>",
        f"Роликов: <b>{len(state.clips)}</b> · общая длительность {duration_text(total)}",
    ]
    if state.trim is not None and (state.trim.head or state.trim.tail):
        lines.append(f"Источник обработан не целиком: {_source_range(state)}")
    if state.archive_name:
        lines.append(f"ZIP: {_escape(state.archive_name)}")
    else:
        lines.append("<i>ZIP не помещается в лимит 50 МБ — ролики отправлены по отдельности.</i>")
    return "\n".join(lines)


def error_text(message: str) -> str:
    return f"❌ {message}"


CANCELLED = "⏹ Нарезку остановил. Уже готовые ролики (если были) остались выше."

# ---------- баннеры ----------

BANNER_SAVED = "✅ Баннер сохранён: <b>{name}</b>."
BANNER_FAILED = "Не получилось принять баннер: {error}"
BANNER_EMPTY = "Баннеров пока нет. Пришлите картинку или видео — добавлю."

# ---------- статистика ----------


def stats_text(clips: int, jobs: int, user_id: int) -> str:
    return (
        "📊 <b>Ваша статистика</b>\n\n"
        f"Роликов нарезано: <b>{clips}</b>\n"
        f"Задач выполнено: <b>{jobs}</b>\n\n"
        f"<i>Ваш ID: <code>{user_id}</code></i>"
    )
