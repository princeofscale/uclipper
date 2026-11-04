"""Тексты бота. Всё по-русски и в одном месте, чтобы их было легко править.

Экраны написаны на rich-HTML Telegram (Bot API 10.1+): заголовки, списки, таблицы, цитаты и
раскрывающиеся блоки — см. `app/bot/rich.py`. Команд (/…) в боте нет, только кнопки, поэтому ни один
текст на них не ссылается. Всё, что приходит снаружи (названия роликов, каналы), экранируется `esc`.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.bot.options import CHOICES
from app.bot.rich import esc
from app.bot.steppers import Stepper
from app.config import MAX_SOURCE_SECONDS
from app.models import ClipSettings, Draft, JobState, JobStatus
from app.services.autoplan import AutoPlan
from app.services.highlights import edge_reserve
from app.services.platforms import EVERYWHERE, PLATFORM_BY_ID, PLATFORMS, SEARCHABLE, TRENDING, Platform
from app.services.search import RESULTS
from app.services.trends import CATEGORIES, PERIOD_TEXT, Category, views_text

NAME = "TgRader Clipper"
PLATFORM_LIST = " · ".join(f"{platform.emoji} {platform.title}" for platform in PLATFORMS)
SEARCH_SITES = ", ".join(f"{platform.emoji} {platform.title}" for platform in SEARCHABLE)
TREND_SITES = " и ".join(f"{platform.emoji} {platform.title}" for platform in TRENDING)
LINK_ONLY_SITES = ", ".join(f"{platform.emoji} {platform.title}" for platform in PLATFORMS if not platform.search)
LINK_ONLY_NOTE = f"{LINK_ONLY_SITES} — только по ссылке: искать на них бот не умеет."
MAX_HOURS = MAX_SOURCE_SECONDS // 3600

# ---------- главное меню и справка ----------

START = f"""\
<h1>🎬 {NAME}</h1>
<p>Нарезаю длинные ролики на вертикальные клипы <b>9:16</b> для <b>TikTok</b>, <b>Shorts</b> и <b>Reels</b> — и вклеиваю в них рекламный баннер.</p>
<p>🌐 Работаю с: {PLATFORM_LIST}</p>
<hr/>
<ul>
<li>🤖 <b>Авто</b> — сам подберу число клипов, длину и самые яркие моменты</li>
<li>🛠 <b>Вручную</b> — каждый параметр в ваших руках</li>
<li>🔥 <b>В тренде</b> — что смотрят прямо сейчас, и сразу в нарезку</li>
</ul>
<blockquote>👇 Пришлите <b>ссылку</b> на ролик, <b>название</b> или <b>видеофайл</b> — дальше всё кнопками.</blockquote>"""

HELP = f"""\
<h2>❓ Как это работает</h2>
<ol>
<li>📥 Пришлите <b>ссылку</b> на ролик, <b>название</b> или <b>видеофайл</b>.</li>
<li>🎛 Выберите режим: <b>🤖 Авто</b> или <b>🛠 Вручную</b>.</li>
<li>🚀 Нажмите <b>«Нарезать»</b> — клипы придут в чат по одному, а если они влезают в 50 МБ, то ещё и одним ZIP.</li>
</ol>
<hr/>
<details><summary>🤖 Что делает авто-режим</summary>
<p>Смотрит на длину ролика и вашу цель (TikTok, Shorts или универсально) и сам выбирает число и длину клипов. Из каждой части ролика берёт самый громкий и «живой» момент — по звуку. Заставку и титры убирает автообрезкой.</p>
<p><i>Это правила и анализ звука, а не нейросеть: зато быстро и предсказуемо. План всегда можно поправить вручную.</i></p>
</details>
<details><summary>🌐 Откуда беру ролики</summary>
<ul>
<li>🔗 <b>По ссылке</b> — {PLATFORM_LIST}.</li>
<li>🔍 <b>По названию</b> — {SEARCH_SITES}: можно выбрать одну площадку или искать везде.</li>
<li>🔥 <b>В тренде</b> — подборки {TREND_SITES}.</li>
</ul>
<p>{LINK_ONLY_NOTE}</p>
<p><i>Берутся только записи — не прямые эфиры, не длиннее {MAX_HOURS} часов и не по платной подписке.</i></p>
</details>
<details><summary>🔥 Раздел «В тренде»</summary>
<p>Самые просматриваемые свежие ролики на русском по жанрам: подкасты, юмор, игры, бизнес, кино, спорт. Площадку можно выбрать — или смотреть везде сразу. Выберите ролик — и сразу получите план нарезки под TikTok.</p>
<p><i>Данные обновляются раз в полчаса.</i></p>
</details>
<details><summary>🎬 Как работает баннер</summary>
<p>В нужный момент клип замирает, играет баннер <b>TGrader</b> (7 секунд, со звуком), и ролик продолжается с того же места. В ручном режиме баннер можно выключить, показать несколько раз, сдвинуть или уменьшить.</p>
</details>
<details><summary>✂️ Обрезка краёв</summary>
<p>Автообрезка убирает заставку, титры и пустые края по звуку и яркости. Если она не нужна — задайте секунды вручную или оставьте ролик целиком.</p>
</details>
<details><summary>📏 Ограничения Telegram</summary>
<ul>
<li>Бот принимает файл до <b>20 МБ</b> — для ролика побольше пришлите ссылку.</li>
<li>Бот отдаёт файл до <b>50 МБ</b> — клип потяжелее он перекодирует в 720×1280.</li>
</ul>
</details>"""

REQUEST_LINK = f"""\
<h3>🔗 Пришлите ссылку</h3>
<p>На ролик с любой из площадок:</p>
<p>{PLATFORM_LIST}</p>
<blockquote>Например: <code>https://rutube.ru/video/…</code> или <code>https://youtu.be/…</code></blockquote>"""

REQUEST_FILE = """\
<h3>📁 Пришлите видеофайл</h3>
<p>Подойдут <b>mp4</b>, <b>mov</b> и <b>mkv</b> — до <b>20 МБ</b>.</p>
<blockquote>Ролик больше? Проще прислать ссылку.</blockquote>"""

NUDGE = "<p>👇 Выберите действие кнопкой — или пришлите <b>ссылку</b>, <b>название</b> или <b>видеофайл</b>.</p>"
NOT_ALLOWED = "🔒 Это закрытый бот.\nВаш ID: <code>{user_id}</code> — передайте его владельцу, чтобы он добавил вас."

FETCHING = "<p>⏳ Смотрю ролик…</p>"

BAD_LINK = f"""\
<h3>🤔 Эту ссылку я не узнаю</h3>
<p>Беру ролики отсюда:</p>
<p>{PLATFORM_LIST}</p>
<p>Нужна ссылка именно на <b>ролик</b> — не на канал, плейлист или главную страницу.</p>
<blockquote>Или просто пришлите <b>название</b> — поищу на {SEARCH_SITES}.</blockquote>
<footer>{LINK_ONLY_NOTE}</footer>"""

DOWNLOADING_FILE = "<p>⏳ Скачиваю видеофайл…</p>"
FILE_TOO_BIG = "<p>📦 Файл больше <b>20 МБ</b> — обычный бот такой не примет.</p><p>Пришлите ссылку на ролик: качество выберем сами.</p>"
FILE_BAD = "<p>😕 Не получилось прочитать видеофайл: {error}</p><p>Пришлите другой файл или ссылку на ролик.</p>"
NOT_A_VIDEO = "<p>🙅 Это не видеофайл. Подойдут <b>.mp4</b>, <b>.mov</b> и <b>.mkv</b> — или пришлите ссылку на ролик.</p>"
BUSY = "<p>⏳ Сейчас идёт нарезка. Дождитесь результата или нажмите <b>«⏹ Остановить»</b> под прогрессом.</p>"

# Короткие подсказки для всплывающих окошек (без разметки: Telegram показывает их как есть).
STALE = "Эта кнопка уже не работает. Начнём заново 👇"
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


def seconds_text(seconds: float) -> str:
    """`7.5` → «7,5 с», `30.0` → «30 с»."""
    return f"{seconds:g}".replace(".", ",") + " с"


def error_text(message: str) -> str:
    return f"<p>❌ {esc(message)}</p>"


# ---------- поиск ----------


def platform_tag(platform_id: str | None) -> str:
    """«📺 RuTube» — откуда ролик. Пусто, если площадка неизвестна (например, ролик прислали файлом)."""
    platform = PLATFORM_BY_ID.get(platform_id or "")
    return f"{platform.emoji} {platform.title}" if platform else ""


def scope_label(choice: str) -> str:
    """Где ищем: «🌐 Везде» или «📺 RuTube»."""
    return "🌐 Везде" if choice == EVERYWHERE else platform_tag(choice)


def request_query(choice: str = EVERYWHERE) -> str:
    """Экран «Что искать?» с выбранной площадкой и честной оговоркой про площадки без поиска."""
    where = f"{scope_label(choice)} ({SEARCH_SITES})" if choice == EVERYWHERE else scope_label(choice)
    return f"""\
<h3>🔍 Что искать?</h3>
<p>Напишите название ролика — покажу до <b>{RESULTS}</b> вариантов.</p>
<p>Где искать: <b>{where}</b>. Площадку можно сменить кнопками ниже.</p>
<blockquote>Можно уточнить: «дуров интервью лекс фридман»</blockquote>
<footer>{LINK_ONLY_NOTE}</footer>"""


def searching(query: str, choice: str = EVERYWHERE) -> str:
    return f"<p>🔍 Ищу ({scope_label(choice)}): <b>{esc(query)}</b>…</p>"


def search_cards(entries: list[dict], choice: str = EVERYWHERE) -> str:
    items = []
    for entry in entries:
        title = esc(entry.get("title") or "Без названия")
        meta = " · ".join(
            part
            for part in (
                platform_tag(entry.get("platform")),
                esc(entry.get("uploader") or ""),
                "⏱ " + duration_text(entry.get("duration") or 0),
            )
            if part
        )
        items.append(f"<li><b>{title}</b><br/><i>{meta}</i></li>")
    return (
        "<h3>🔍 Нашёл такие ролики</h3><p>Выберите нужный:</p><ol>"
        + "".join(items)
        + f"</ol><footer>{scope_label(choice)} · тот же запрос на другой площадке — кнопками ниже</footer>"
    )


# ---------- карточка ролика ----------


def video_card(title: str, uploader: str | None, duration: float, platform: str | None = None) -> str:
    """Название, автор, длина и площадка ролика — цитатой, чтобы карточка отличалась от остального экрана."""
    meta = [
        part
        for part in (
            f"👤 {esc(uploader)}" if uploader else "",
            f"⏱ {duration_text(duration)}" if duration else "",
            platform_tag(platform),
        )
        if part
    ]
    line = f"<br/>{' · '.join(meta)}" if meta else ""
    return f"<blockquote><b>{esc(title)}</b>{line}</blockquote>"


def _card(draft: Draft) -> str:
    return video_card(draft.title, draft.uploader, draft.duration, draft.platform)


def mode_screen(draft: Draft) -> str:
    """Первый экран после выбора ролика: как будем нарезать."""
    return f"""\
<h2>🎬 Ролик принят</h2>
{_card(draft)}
<h3>Как будем нарезать?</h3>
<ul>
<li>🤖 <b>Авто</b> — подберу число клипов, длину и яркие моменты под этот ролик. <i>Быстрее всего.</i></li>
<li>🛠 <b>Вручную</b> — вы сами решаете всё: сколько клипов, какой длины, какой кадр и баннер.</li>
</ul>"""


def clip_total(settings: ClipSettings, banner_seconds: float | None) -> float:
    """Длина готового клипа: видео плюс паузы под баннер (если он останавливает клип)."""
    if settings.banner_count and banner_seconds and settings.banner_mode.value == "freeze":
        return settings.clip_duration + settings.banner_count * banner_seconds
    return settings.clip_duration


def _total_line(settings: ClipSettings, banner_seconds: float | None) -> str:
    total = clip_total(settings, banner_seconds)
    if total > settings.clip_duration:
        return (
            f"≈ <b>{seconds_text(round(total, 1))}</b> "
            f"({seconds_text(settings.clip_duration)} видео + {settings.banner_count} × {seconds_text(banner_seconds or 0)} баннер)"
        )
    return f"≈ <b>{seconds_text(settings.clip_duration)}</b>"


def trim_text(s: ClipSettings) -> str:
    if s.auto_trim:
        return "авто: уберу заставку и титры"
    parts = []
    if s.start_offset:
        parts.append(f"начало −{s.start_offset:g} с")
    if s.end_offset:
        parts.append(f"конец −{s.end_offset:g} с")
    return ", ".join(parts) or "не обрезаю, беру целиком"


def _rough(seconds: float) -> float:
    """До 5 секунд: в плане стоит «≈», а 200,7 с и 3:21 выглядели бы точнее, чем это есть."""
    return round(seconds / 5) * 5


def _frame_text(s: ClipSettings) -> str:
    return CHOICES["frame_mode"].option(s.frame_mode).short


def auto_screen(draft: Draft, plan: AutoPlan, banner_seconds: float | None) -> str:
    """План автоподбора: что бот собирается сделать и почему так."""
    s = plan.settings
    banner = (
        f"TGrader · {s.banner_count} × {seconds_text(banner_seconds or 0)}, со звуком"
        if s.banner_count and banner_seconds
        else "выключен"
    )
    short_note = "<p>ℹ️ Ролик короткий — беру его целиком.</p>" if plan.shortened else ""
    head, tail = edge_reserve(draft.duration, s.num_clips * s.clip_duration)
    skipped = (
        f"<tr><td>🚫 Пропускаю</td><td>начало (≈ {duration_text(_rough(head))}) — заставка и реклама, "
        f"конец (≈ {duration_text(_rough(tail))}) — титры</td></tr>"
        if head or tail
        else ""
    )
    return f"""\
<h2>🤖 Авто-подбор</h2>
{_card(draft)}
<h3>План нарезки</h3>
<table bordered>
<tr><td>🎞 Клипов</td><td><b>{s.num_clips}</b> по <b>{seconds_text(s.clip_duration)}</b></td></tr>
<tr><td>⏱ Итог на клип</td><td>{_total_line(s, banner_seconds)}</td></tr>
<tr><td>✨ Моменты</td><td>самые яркие, по звуку</td></tr>
{skipped}<tr><td>🖼 Кадр</td><td>{esc(_frame_text(s))}</td></tr>
<tr><td>✂️ Края</td><td>уберу чёрный экран и титры</td></tr>
<tr><td>🎬 Баннер</td><td>{banner}</td></tr>
</table>
{short_note}<blockquote>{plan.spec.emoji} <b>{plan.spec.label}</b> — {plan.spec.blurb}</blockquote>
<footer>Цель можно сменить кнопками ниже, а любую мелочь — поправить вручную.</footer>"""


def manual_screen(draft: Draft, banner_seconds: float | None) -> str:
    """Ручная настройка: сводка всех значений. Кнопки под текстом меняют их по одной."""
    s = draft.settings
    cut = CHOICES["slicing_mode"].option(s.slicing_mode).short
    if s.banner_count:
        banner = f"""\
<h3>🎬 Баннер TGrader</h3>
<table compact>
<tr><td>Показов</td><td><b>{s.banner_count}</b> за клип</td></tr>
<tr><td>Показ</td><td>{CHOICES["banner_mode"].option(s.banner_mode).short}</td></tr>
<tr><td>Где</td><td>{CHOICES["banner_position"].option(s.banner_position).short}</td></tr>
<tr><td>Размер</td><td>{s.banner_scale}% ширины</td></tr>
<tr><td>Звук</td><td>{"включён" if s.banner_sound else "выключен"}</td></tr>
</table>"""
    else:
        banner = "<h3>🎬 Баннер</h3><p>Выключен — клипы пойдут без рекламной вставки.</p>"
    return f"""\
<h2>🛠 Ручная настройка</h2>
{_card(draft)}
<h3>✂️ Нарезка</h3>
<table compact>
<tr><td>Клипов</td><td><b>{s.num_clips}</b> × <b>{seconds_text(s.clip_duration)}</b></td></tr>
<tr><td>Откуда</td><td>{cut}</td></tr>
<tr><td>Кадр</td><td>{esc(_frame_text(s))}</td></tr>
<tr><td>Края ролика</td><td>{trim_text(s)}</td></tr>
</table>
{banner}
<footer>⏱ Каждый клип выйдет {_total_line(s, banner_seconds)} · качество скачивания до {s.max_height}p</footer>"""


def choice_screen(field: str, current: object) -> str:
    """Экран выбора из вариантов: все варианты с объяснением, текущий отмечен."""
    choice = CHOICES[field]
    selected = choice.option(current).value
    items = "".join(
        f"<li>{'✅' if option.value == selected else option.icon} <b>{esc(option.label)}</b> — {esc(option.hint)}</li>"
        for option in choice.options
    )
    return f"<h2>{choice.icon} {choice.title}</h2><p>{choice.intro}</p><ul>{items}</ul>"


def stepper_screen(stepper: Stepper, value: float) -> str:
    return (
        f"<h2>{esc(stepper.title)}</h2>"
        f"<p>{esc(stepper.hint)}</p>"
        f"<p>Сейчас: <b>{esc(stepper.show(value))}</b> <i>({stepper.range_text()})</i></p>"
    )


def stepper_prompt(stepper: Stepper, value: float) -> str:
    return (
        f"<h3>✍️ {esc(stepper.title)}</h3>"
        f"<p>Сейчас: <b>{esc(stepper.show(value))}</b></p>"
        f"<p>Напишите число {stepper.range_text()}" + (" — целое." if stepper.integer else ".") + "</p>"
    )


def stepper_invalid(stepper: Stepper, value: float | None = None) -> str:
    """Подсказка, когда ввод не подошёл. `None` — это вообще не число."""
    if value is not None and stepper.integer and value != int(value):
        return f"<p>🔢 Здесь нужно целое число {stepper.range_text()}.</p>"
    return f"<p>🔢 Нужно число {stepper.range_text()}. Напишите другое.</p>"


def more_screen(s: ClipSettings) -> str:
    return f"""\
<h2>⚙️ Дополнительно</h2>
<table compact>
<tr><td>📥 Качество скачивания</td><td><b>{CHOICES["max_height"].option(s.max_height).short}</b></td></tr>
<tr><td>🔔 Сигнал перед баннером</td><td><b>{"включён" if s.enable_sfx else "выключен"}</b></td></tr>
</table>
<blockquote>Чем выше качество, тем чётче клипы — но дольше скачивание и тяжелее файлы. Сигнал — короткий «бульк» за мгновение до баннера.</blockquote>"""


def trim_screen(s: ClipSettings) -> str:
    return f"""\
<h2>✂️ Края ролика</h2>
<table compact>
<tr><td>Автообрезка</td><td><b>{"включена" if s.auto_trim else "выключена"}</b></td></tr>
<tr><td>Пропустить в начале</td><td>{s.start_offset:g} с</td></tr>
<tr><td>Пропустить в конце</td><td>{s.end_offset:g} с</td></tr>
</table>
<blockquote>Автообрезка сама убирает заставку, титры и пустые края по звуку и яркости. Когда она включена, секунды вручную не используются. Без обрезки беру ролик целиком.</blockquote>"""


# ---------- прогресс и результат ----------

BAR_WIDTH = 20


def progress_bar(percent: float) -> str:
    filled = max(0, min(BAR_WIDTH, round(percent / 100 * BAR_WIDTH)))
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


QUEUED_TEXT = "<h3>⏳ Задача в очереди</h3><p>Начну, как только освободится сервер.</p>"


def progress_text(state: JobState, ahead: int = 0) -> str:
    """Сообщение-прогресс: стадия, полоса, счётчик клипов и (свёрнуто) последние строки лога.

    `ahead` — сколько задач впереди; показывается только у ждущей очереди задачи.
    """
    head = {
        JobStatus.QUEUED: "⏳ Задача в очереди",
        JobStatus.RUNNING: f"⚙️ {esc(state.stage)}",
        JobStatus.DONE: "✅ Готово",
        JobStatus.ERROR: "❌ Ошибка",
        JobStatus.CANCELLED: "⏹ Отменено",
    }[state.status]
    parts = [f"<h3>{head}</h3>"]
    if state.status is JobStatus.QUEUED and ahead > 0:
        parts.append(f"<p>🧍 Перед вами в очереди: <b>{ahead}</b>. Начну, как только дойдёт очередь.</p>")
    parts.append(f"<p><code>{progress_bar(state.progress)}</code> <b>{state.progress:.0f}%</b></p>")
    if state.clips:
        parts.append(f"<p>🎞 Клипов готово: <b>{len(state.clips)}</b></p>")
    trim = state.trim
    if trim is not None and (trim.head or trim.tail):
        parts.append(f"<p><i>✂️ Обрезал: {esc(' · '.join(part for part in (trim.head, trim.tail) if part))}</i></p>")
    tail = [line for line in state.log[-3:] if line]
    if tail and state.status is JobStatus.RUNNING:
        parts.append("<details><summary>📜 Что происходит</summary><p>" + "<br/>".join(esc(line) for line in tail) + "</p></details>")
    return "".join(parts)


def _source_range(state: JobState) -> str:
    trim = state.trim
    if trim is None:
        return "весь"
    return f"с {duration_text(trim.start)} по {duration_text(trim.end)}"


def results_summary(state: JobState) -> str:
    """Итог задачи: сколько роликов, длительность, что с архивом."""
    total = sum(clip.duration for clip in state.clips)
    rows = [
        f"<tr><td>🎞 Клипов</td><td><b>{len(state.clips)}</b></td></tr>",
        f"<tr><td>⏱ Общая длительность</td><td>{duration_text(total)}</td></tr>",
    ]
    if state.trim is not None and (state.trim.head or state.trim.tail):
        rows.append(f"<tr><td>✂️ Источник</td><td>{esc(_source_range(state))}</td></tr>")
    note = (
        f"<p>📦 ZIP: <code>{esc(state.archive_name)}</code></p>"
        if state.archive_name
        else "<p><i>ZIP не помещается в лимит 50 МБ — ролики отправлены по отдельности.</i></p>"
    )
    return f"<h2>✅ Готово!</h2><table bordered compact>{''.join(rows)}</table>{note}"


CANCELLED = "<p>⏹ Нарезку остановил. Уже готовые ролики (если были) остались выше.</p>"

# ---------- баннеры ----------

BANNER_EMPTY = "<h2>🎬 Баннеры</h2><p>Баннера пока нет — положите файл <code>TGrader-banner-7s.mp4</code> в <code>assets/banners</code>.</p>"


def banners_screen(banner) -> str:
    """Экран «Баннеры»: единственный баннер проекта и как он вставляется."""
    if banner is None:
        return BANNER_EMPTY
    seconds = seconds_text(round(banner.duration or 0, 1))
    sound = "есть 🔊" if banner.has_audio else "нет 🔇"
    return f"""\
<h1>🎬 Баннеры</h1>
<p>Рекламная вставка, которую я вклеиваю в ваши клипы.</p>
<hr/>
<h3>📺 TGrader</h3>
<table bordered>
<tr><td>⏱ Длительность</td><td><b>{seconds}</b></td></tr>
<tr><td>🔊 Звук</td><td>{sound}</td></tr>
<tr><td>🎞 Формат</td><td>видео {banner.width}×{banner.height}</td></tr>
</table>
<blockquote>Как это работает: в нужный момент клип замирает, играет баннер ({seconds}, со звуком) — и ролик продолжается с того же места.</blockquote>
<footer>Баннер подключён автоматически. В ручном режиме его можно выключить, показать несколько раз или сдвинуть.</footer>"""


BANNER_PREVIEW_CAPTION = "<b>TGrader</b> · {seconds} · со звуком"

# ---------- тренды ----------


def trends_loading(category: Category, choice: str = EVERYWHERE) -> str:
    where = "на всех площадках" if choice == EVERYWHERE else f"на {scope_label(choice)}"
    return (
        f"<h2>{category.emoji} {esc(category.title)}</h2>"
        f"<p>🔥 Смотрю, что сейчас в тренде {where}…</p>"
        "<footer>В первый раз это занимает несколько секунд</footer>"
    )


def trends_home(choice: str = EVERYWHERE) -> str:
    items = "".join(
        f"<li>{category.emoji} <b>{esc(category.title)}</b> — {esc(category.hint)}</li>" for category in CATEGORIES
    )
    return f"""\
<h1>🔥 В тренде</h1>
<p>Что <b>прямо сейчас</b> смотрят русскоязычные зрители. Выберите площадку и жанр — покажу самые просматриваемые ролики, которые стоит нарезать на <b>TikTok</b> и <b>Shorts</b>.</p>
<ul>{items}</ul>
<footer>{scope_label(choice)}: данные {PERIOD_TEXT.get(choice, "за последнее время")} · обновляются раз в полчаса</footer>"""


def trend_list(category: Category, videos: list[dict], choice: str = EVERYWHERE, *, missing: Sequence[Platform] = ()) -> str:
    """Список трендов жанра. `missing` — площадки, которые сейчас не ответили (на них показывать нечего, но скрывать это нечестно)."""
    head = f"<h2>{category.emoji} {esc(category.title)}</h2>"
    notes = [f"⚠️ {platform.title} сейчас не ответил — показываю остальные." for platform in missing]
    if category.note:
        notes.append(category.note)
    note = f"<footer>{'<br/>'.join(esc(line) for line in notes)}</footer>" if notes else ""
    if not videos:
        return (
            f"{head}<p>🫥 {scope_label(choice)}: сейчас ничего подходящего не нашлось. "
            f"Загляните позже, выберите другой жанр или другую площадку.</p>{note}"
        )
    items = []
    for video in videos:
        meta = " · ".join(
            part
            for part in (
                platform_tag(video.get("platform")) if choice == EVERYWHERE else "",
                esc(video.get("uploader") or ""),
                "⏱ " + duration_text(video.get("duration") or 0),
                "👁 " + views_text(int(video.get("views") or 0)),
            )
            if part
        )
        items.append(f"<li><b>{esc(video['title'])}</b><br/><i>{meta}</i></li>")
    period = PERIOD_TEXT.get(choice, "за последнее время")
    return (
        f"{head}"
        f"<p>{scope_label(choice)} · самое просматриваемое {period} — {esc(category.hint)}.</p>"
        f"<ol>{''.join(items)}</ol>"
        "<blockquote>Выберите ролик — сразу подберу нарезку под TikTok.</blockquote>"
        f"{note}"
    )


# ---------- статистика ----------


def stats_text(clips: int, jobs: int, user_id: int) -> str:
    return f"""\
<h2>📊 Ваша статистика</h2>
<table bordered>
<tr><td>🎞 Роликов нарезано</td><td><b>{clips}</b></td></tr>
<tr><td>✅ Задач выполнено</td><td><b>{jobs}</b></td></tr>
</table>
<footer>Ваш ID: <code>{user_id}</code></footer>"""
