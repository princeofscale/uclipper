"""Тексты бота. Все сообщения — по-русски и в одном месте, чтобы их было легко править."""
from __future__ import annotations

from app.models import BannerMode, BannerPosition, FrameMode, JobState, JobStatus, SlicingMode, StaticPosition

HELP = """\
<b>ClipBanner Bot</b> — нарезка YouTube-роликов на вертикальные клипы с рекламным баннером.

<b>Как это работает</b>
1. Пришлите ссылку на YouTube, название ролика или сам видеофайл.
2. Настройте клипы и баннер в меню — всё кнопками.
3. Нажмите «Нарезать» и заберите готовые ролики. Если все они влезают в 50 МБ, бот дополнительно пришлёт ZIP.

<b>Команды</b>
/start — начать заново
/new — новый ролик
/settings — текущие настройки
/banners — список баннеров и загрузка своего
/mystats — сколько вы уже нарезали
/help — эта справка

<b>Ограничения Telegram</b>
• Прислать боту файл можно до 20 МБ — если ролик больше, пришлите ссылку на YouTube.
• Бот отдаёт файлы до 50 МБ; клип побольше он перекодирует в 720×1280.
"""

START = """\
Привет! Это <b>ClipBanner Bot</b> — нарежу вертикальные клипы из YouTube-ролика и вклею баннер.

Что прислать:
• <b>ссылку</b> на YouTube — например, https://youtu.be/…
• <b>название</b> ролика — найду сам, покажу варианты
• <b>видеофайл</b> — до 20 МБ (обычно это 4–5 минут в 720p)

Просто пришлите ссылку, название или файл — дальше покажу настройки.
Подробнее — /help.
"""

NEW = "Окей, новый ролик. Пришлите ссылку на YouTube, название или видеофайл."

REQUEST_LINK = "Пришлите ссылку на YouTube-ролик — например, https://youtu.be/dQw4w9WgXcQ"

REQUEST_QUERY = (
    "Напишите название ролика — найду на YouTube до 5 вариантов.\n"
    "<i>Можно уточнить: «дуров интервью лекс фридман»</i>"
)

REQUEST_FILE = (
    "Пришлите видеофайл (mp4, mov или mkv) — до 20 МБ.\n"
    "<i>Если ролик больше, проще прислать ссылку на YouTube.</i>"
)

REQUEST_BANNER = (
    "Пришлите баннер: картинку (png, jpg, webp) или видео (mp4, mov, webm) до 20 секунд.\n"
    "<i>Отмена — /settings</i>"
)

SEARCHING = "Ищу на YouTube: <b>{query}</b>…"
SEARCH_NOTHING = "Ничего не нашлось. Попробуйте другое название или пришлите ссылку."
FETCHING = "Смотрю ролик…"

BAD_LINK = (
    "Это не похоже на ссылку на YouTube-ролик.\n\n"
    "Подойдут: https://www.youtube.com/watch?v=…, https://youtu.be/…, /shorts/…\n"
    "Либо пришлите название ролика — поищу."
)

DOWNLOADING_FILE = "Скачиваю видеофайл…"
FILE_TOO_BIG = "Файл больше 20 МБ — обычный бот не примет его. Пришлите ссылку на YouTube: качество выберем сами."
FILE_BAD = "Не получилось прочитать видеофайл: {error}\nПришлите другой файл или ссылку на YouTube."

SOURCE_UPLOAD = "ваш файл"
SOURCE_YOUTUBE = "YouTube"


def duration_text(seconds: float) -> str:
    """`95` → «1:35», `3725` → «1:02:05»."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def video_card(draft_title: str, uploader: str | None, duration: float, clips: int) -> str:
    """Карточка выбранного ролика над экраном настроек."""
    lines = [f"<b>{_escape(draft_title)}</b>"]
    meta = []
    if uploader:
        meta.append(_escape(uploader))
    if duration:
        meta.append(duration_text(duration))
    if meta:
        lines.append(" • ".join(meta))
    lines.append(f"<i>Планирую клипов: {clips}</i>")
    return "\n".join(lines)


def _escape(text: str) -> str:
    """Экранирует HTML-спецсимволы: названия роликов часто содержат `<`, `&` и кавычки."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


FRAME_LABELS = {
    FrameMode.BLUR_PAD: "9:16 размытый фон",
    FrameMode.CENTER_CROP: "9:16 кроп",
    FrameMode.ORIGINAL: "16:9 как есть",
}
SLICING_LABELS = {SlicingMode.EVENLY_SPACED: "равномерно", SlicingMode.SEQUENTIAL: "подряд"}
BANNER_MODE_LABELS = {BannerMode.FREEZE: "пауза", BannerMode.OVERLAY: "поверх"}
POSITION_LABELS = {BannerPosition.CENTER: "центр", BannerPosition.TOP: "верх", BannerPosition.BOTTOM: "низ"}
STATIC_POSITION_LABELS = {StaticPosition.TOP: "верхняя", StaticPosition.BOTTOM: "нижняя"}


def settings_screen(draft, banner_name: str | None, static_name: str | None) -> str:
    """Экран настроек: карточка ролика и все текущие значения."""
    s = draft.settings
    with_banner = s.banner_count > 0
    banner_line = ""
    if with_banner:
        banner = f"<b>{_escape(banner_name or s.banner_id or '—')}</b>"
        pause = s.banner_duration
        banner_line = (
            f"\n🎬 Баннер: {banner}\n"
            f"   показов на клип: {s.banner_count} · {BANNER_MODE_LABELS[s.banner_mode]} · "
            f"позиция: {POSITION_LABELS[s.banner_position]} · {s.banner_scale}%"
        )
    else:
        banner_line = "\n🎬 Баннер: <b>выключен</b>"
    static_line = (
        f"\n📌 Постоянный: <b>{_escape(static_name or s.static_banner_id or '—')}</b> · "
        f"{STATIC_POSITION_LABELS[s.static_position]} · {s.static_scale}%"
        if s.static_banner_id
        else "\n📌 Постоянный баннер: выключен"
    )
    if s.auto_trim:
        trim = "авто (начало и конец ищет программа)"
    elif s.start_offset or s.end_offset:
        parts = []
        if s.start_offset:
            parts.append(f"начало {s.start_offset:g} с")
        if s.end_offset:
            parts.append(f"конец {s.end_offset:g} с")
        trim = ", ".join(parts)
    else:
        trim = "нет"
    return VIDEO_CARD_SLOT.format(
        card=video_card(draft.title, draft.uploader, draft.duration, s.num_clips),
        num_clips=s.num_clips,
        clip_duration=f"{s.clip_duration:g} с",
        slicing=SLICING_LABELS[s.slicing_mode],
        frame=FRAME_LABELS[s.frame_mode],
        trim=trim,
        banner_line=banner_line + static_line,
        quality=s.max_height,
    )


VIDEO_CARD_SLOT = """\
{card}

✂️ <b>Клипы</b>
   количество: {num_clips}
   длительность: {clip_duration}
   раскладка: {slicing}
   кадр: {frame}
   обрезка: {trim} · кнопка ещё раз

🎛 <b>Баннер</b>{banner_line}

📥 Качество скачивания: до {quality}p

<i>«Ещё» — автообрезка, частая нарезка, качество.</i>"""


def progress_text(state: JobState) -> str:
    """Сообщение-прогресс: стадия, полоса, счётчик клипов и последние строки лога."""
    filled = round(state.progress / 5)  # 20 клеток на 100%
    bar = "█" * min(20, filled) + "▁" * (20 - min(20, filled))
    head = {
        JobStatus.QUEUED: "⏳ Задача в очереди",
        JobStatus.RUNNING: f"⚙️ {state.stage}",
        JobStatus.DONE: "✅ Готово",
        JobStatus.ERROR: "❌ Ошибка",
        JobStatus.CANCELLED: "⏹ Отменено",
    }[state.status]
    lines = [f"<b>{head}</b>", f"<code>{bar}</code> {state.progress:.0f}%"]
    done = len(state.clips)
    if done:
        lines.append(f"Роликов готово: {done}")
    if state.trim is not None:
        trim = state.trim
        if trim.head or trim.tail:
            lines.append(f"<i>Обрезал: {trim.head or ''}{', ' if trim.head and trim.tail else ''}{trim.tail or ''}</i>")
    tail = [line for line in state.log[-3:] if line]
    if tail and state.status is JobStatus.RUNNING:
        lines.append("")
        lines.extend(f"<i>{_escape(line)}</i>" for line in tail)
    return "\n".join(lines)


def results_summary(state: JobState) -> str:
    """Итог задачи: сколько роликов, длительность, что с архивом."""
    total = sum(clip.duration for clip in state.clips)
    lines = [
        "<b>Готово!</b>",
        f"Роликов: {len(state.clips)} · общая длительность: {duration_text(total)}",
    ]
    if state.trim is not None and (state.trim.head or state.trim.tail):
        lines.append(f"Источник обработан не целиком: {_source_range(state)}")
    if state.archive_name:
        lines.append(f"ZIP: {_escape(state.archive_name)}")
    else:
        lines.append("ZIP не помещается в лимит 50 МБ — ролики отправил по отдельности.")
    lines.append("")
    lines.append("<i>«Новый ролик» — начать заново, /settings — поменять настройки для следующего.</i>")
    return "\n".join(lines)


def _source_range(state: JobState) -> str:
    trim = state.trim
    if trim is None:
        return "весь"
    return f"с {duration_text(trim.start)} по {duration_text(trim.end)}"


def clips_summary(state: JobState) -> str:
    """Что именно вырезано (для подписи под последним роликом)."""
    return "\n".join(
        f"{clip.index}. {duration_text(clip.source_start)}–{duration_text(clip.source_end)} → {duration_text(clip.duration)}"
        for clip in state.clips
    )


def error_text(message: str) -> str:
    return f"❌ {message}"


CANCELLED = "⏹ Нарезку остановил. Уже готовые ролики (если были) остались выше."
NOTHING_TO_CANCEL = "Сейчас ничего не выполняется."

BANNER_SAVED = "Баннер сохранён: <b>{name}</b>. Теперь выберите его в настройках."
BANNER_FAILED = "Не получилось принять баннер: {error}"
BANNER_PICK = "Выберите баннер для клипов. Баннеры с пометкой 🎞 — видео со звуком."
BANNER_EMPTY = "Баннеров пока нет. Пришлите картинку или видео — добавлю."
SETTINGS_SAVED = "Настройки сохранены — они применятся к следующей нарезке."

STATS = """\
<b>Ваша статистика</b>
Роликов нарезано: {clips}
Задач выполнено: {jobs}
Последняя нарезка: {last}
"""


def stats_text(clips: int, jobs: int, last: str | None) -> str:
    return STATS.format(clips=clips, jobs=jobs, last=last or "ещё не было")
