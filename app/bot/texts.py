"""Тексты бота. Всё по-русски и в одном месте, чтобы их было легко править.

Экраны написаны на rich-HTML Telegram (Bot API 10.1+): заголовки, списки, таблицы, цитаты и
раскрывающиеся блоки — см. `app/bot/rich.py`. Команд (/…) в боте нет, только кнопки, поэтому ни один
текст на них не ссылается. Всё, что приходит снаружи (названия роликов, каналы), экранируется `esc`.
"""
from __future__ import annotations

from app.bot.options import CHOICES
from app.bot.rich import esc
from app.bot.steppers import Stepper
from app.models import ClipSettings, Draft, JobState, JobStatus
from app.services.autoplan import AutoPlan
from app.services.trends import CATEGORIES, Category, views_text

NAME = "TgRader Clipper"

# ---------- главное меню и справка ----------

START = f"""\
<h1>🎬 {NAME}</h1>
<p>Нарезаю длинные YouTube-ролики на вертикальные клипы <b>9:16</b> для <b>TikTok</b>, <b>Shorts</b> и <b>Reels</b> — и вклеиваю в них рекламный баннер.</p>
<hr/>
<ul>
<li>🤖 <b>Авто</b> — сам подберу число клипов, длину и самые яркие моменты</li>
<li>🛠 <b>Вручную</b> — каждый параметр в ваших руках</li>
<li>🔥 <b>В тренде</b> — что смотрят прямо сейчас, и сразу в нарезку</li>
</ul>
<blockquote>👇 Пришлите <b>ссылку</b> на YouTube, <b>название</b> ролика или <b>видеофайл</b> — дальше всё кнопками.</blockquote>"""

HELP = """\
<h2>❓ Как это работает</h2>
<ol>
<li>📥 Пришлите <b>ссылку</b> на YouTube, <b>название</b> ролика или <b>видеофайл</b>.</li>
<li>🎛 Выберите режим: <b>🤖 Авто</b> или <b>🛠 Вручную</b>.</li>
<li>🚀 Нажмите <b>«Нарезать»</b> — клипы придут в чат по одному, а если они влезают в 50 МБ, то ещё и одним ZIP.</li>
</ol>
<hr/>
<details><summary>🤖 Что делает авто-режим</summary>
<p>Смотрит на длину ролика и вашу цель (TikTok, Shorts или универсально) и сам выбирает число и длину клипов. Из каждой части ролика берёт самый громкий и «живой» момент — по звуку. Заставку и титры убирает автообрезкой.</p>
<p><i>Это правила и анализ звука, а не нейросеть: зато быстро и предсказуемо. План всегда можно поправить вручную.</i></p>
</details>
<details><summary>🔥 Раздел «В тренде»</summary>
<p>Самые просматриваемые за неделю ролики на русском по жанрам: подкасты, юмор, игры, бизнес, кино, спорт. Выберите ролик — и сразу получите план нарезки под TikTok.</p>
<p><i>Данные берутся с YouTube и обновляются раз в полчаса.</i></p>
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

REQUEST_LINK = """\
<h3>🔗 Пришлите ссылку</h3>
<p>На обычный ролик, <code>youtu.be</code> или Shorts.</p>
<blockquote>Например: <code>https://youtu.be/dQw4w9WgXcQ</code></blockquote>"""

REQUEST_QUERY = """\
<h3>🔍 Что искать?</h3>
<p>Напишите название ролика — найду на YouTube до <b>5</b> вариантов.</p>
<blockquote>Можно уточнить: «дуров интервью лекс фридман»</blockquote>"""

REQUEST_FILE = """\
<h3>📁 Пришлите видеофайл</h3>
<p>Подойдут <b>mp4</b>, <b>mov</b> и <b>mkv</b> — до <b>20 МБ</b>.</p>
<blockquote>Ролик больше? Проще прислать ссылку на YouTube.</blockquote>"""

NUDGE = "<p>👇 Выберите действие кнопкой — или пришлите <b>ссылку</b>, <b>название</b> или <b>видеофайл</b>.</p>"
NOT_ALLOWED = "🔒 Это закрытый бот.\nВаш ID: <code>{user_id}</code> — передайте его владельцу, чтобы он добавил вас."

SEARCHING = "<p>🔍 Ищу на YouTube: <b>{query}</b>…</p>"
SEARCH_NOTHING = "<p>🫥 По запросу «{query}» ничего не нашлось.</p><p>Попробуйте другое название или пришлите ссылку на YouTube.</p>"
FETCHING = "<p>⏳ Смотрю ролик…</p>"

BAD_LINK = """\
<h3>🤔 Это не похоже на ссылку на YouTube</h3>
<p>Подойдут такие:</p>
<ul>
<li><code>youtube.com/watch?v=…</code></li>
<li><code>youtu.be/…</code></li>
<li><code>youtube.com/shorts/…</code></li>
</ul>
<p>Или просто пришлите <b>название</b> ролика — поищу.</p>"""

DOWNLOADING_FILE = "<p>⏳ Скачиваю видеофайл…</p>"
FILE_TOO_BIG = "<p>📦 Файл больше <b>20 МБ</b> — обычный бот такой не примет.</p><p>Пришлите ссылку на YouTube: качество выберем сами.</p>"
FILE_BAD = "<p>😕 Не получилось прочитать видеофайл: {error}</p><p>Пришлите другой файл или ссылку на YouTube.</p>"
NOT_A_VIDEO = "<p>🙅 Это не видеофайл. Подойдут <b>.mp4</b>, <b>.mov</b> и <b>.mkv</b> — или пришлите ссылку на YouTube.</p>"
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


def search_cards(entries: list[dict]) -> str:
    items = []
    for entry in entries:
        title = esc(entry.get("title") or "Без названия")
        meta = " · ".join(
            part
            for part in (esc(entry.get("uploader") or ""), "⏱ " + duration_text(entry.get("duration") or 0))
            if part
        )
        items.append(f"<li><b>{title}</b><br/><i>{meta}</i></li>")
    return "<h3>🔍 Нашёл такие ролики</h3><p>Выберите нужный:</p><ol>" + "".join(items) + "</ol>"


# ---------- карточка ролика ----------


def video_card(title: str, uploader: str | None, duration: float) -> str:
    """Название, автор и длина ролика — цитатой, чтобы карточка отличалась от остального экрана."""
    meta = [part for part in (f"👤 {esc(uploader)}" if uploader else "", f"⏱ {duration_text(duration)}" if duration else "") if part]
    line = f"<br/>{' · '.join(meta)}" if meta else ""
    return f"<blockquote><b>{esc(title)}</b>{line}</blockquote>"


def _card(draft: Draft) -> str:
    return video_card(draft.title, draft.uploader, draft.duration)


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
    return f"""\
<h2>🤖 Авто-подбор</h2>
{_card(draft)}
<h3>План нарезки</h3>
<table bordered>
<tr><td>🎞 Клипов</td><td><b>{s.num_clips}</b> по <b>{seconds_text(s.clip_duration)}</b></td></tr>
<tr><td>⏱ Итог на клип</td><td>{_total_line(s, banner_seconds)}</td></tr>
<tr><td>✨ Моменты</td><td>самые яркие, по звуку</td></tr>
<tr><td>🖼 Кадр</td><td>{esc(_frame_text(s))}</td></tr>
<tr><td>✂️ Края</td><td>уберу заставку и титры</td></tr>
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


def progress_text(state: JobState) -> str:
    """Сообщение-прогресс: стадия, полоса, счётчик клипов и (свёрнуто) последние строки лога."""
    head = {
        JobStatus.QUEUED: "⏳ Задача в очереди",
        JobStatus.RUNNING: f"⚙️ {esc(state.stage)}",
        JobStatus.DONE: "✅ Готово",
        JobStatus.ERROR: "❌ Ошибка",
        JobStatus.CANCELLED: "⏹ Отменено",
    }[state.status]
    parts = [f"<h3>{head}</h3>", f"<p><code>{progress_bar(state.progress)}</code> <b>{state.progress:.0f}%</b></p>"]
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


def trends_loading(category: Category) -> str:
    return (
        f"<h2>{category.emoji} {esc(category.title)}</h2>"
        "<p>🔥 Смотрю, что сейчас в тренде…</p>"
        "<footer>В первый раз это занимает несколько секунд</footer>"
    )


def trends_home() -> str:
    items = "".join(
        f"<li>{category.emoji} <b>{esc(category.title)}</b> — {esc(category.hint)}</li>" for category in CATEGORIES
    )
    return f"""\
<h1>🔥 В тренде</h1>
<p>Что <b>прямо сейчас</b> смотрят русскоязычные зрители. Выберите жанр — покажу самые просматриваемые ролики недели, которые стоит нарезать на <b>TikTok</b> и <b>Shorts</b>.</p>
<ul>{items}</ul>
<footer>Данные YouTube за неделю · обновляются раз в полчаса</footer>"""


def trend_list(category: Category, videos: list[dict]) -> str:
    if not videos:
        return (
            f"<h2>{category.emoji} {esc(category.title)}</h2>"
            "<p>🫥 Сейчас ничего подходящего не нашлось. Загляните позже или выберите другой жанр.</p>"
        )
    items = []
    for video in videos:
        meta = " · ".join(
            part
            for part in (
                esc(video.get("uploader") or ""),
                "⏱ " + duration_text(video.get("duration") or 0),
                "👁 " + views_text(int(video.get("views") or 0)),
            )
            if part
        )
        items.append(f"<li><b>{esc(video['title'])}</b><br/><i>{meta}</i></li>")
    return (
        f"<h2>{category.emoji} {esc(category.title)}</h2>"
        f"<p>Самое просматриваемое за неделю — {esc(category.hint)}.</p>"
        f"<ol>{''.join(items)}</ol>"
        "<blockquote>Выберите ролик — сразу подберу нарезку под TikTok.</blockquote>"
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
