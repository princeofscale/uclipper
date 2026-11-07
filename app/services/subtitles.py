"""Субтитры: слова площадки → строки → файл `.ass` с караоке.

Зачем: субтитры — единственная правка, которую площадки считают содержательной («slight modification»
по правилам TikTok), и их смотрят без звука. Считать их самим дорого (модель не влезает в память бота),
поэтому первый путь — **готовые субтитры площадки**: `yt-dlp --write-auto-subs --sub-format json3`
отдаёт слова с таймстемпами бесплатно.

Модуль чистый: ни сети, ни ffmpeg, ни состояния. Вход — JSON от площадки, выход — текст `.ass`.
Вшивание в видео — отдельный шаг (`ass` фильтр в том же проходе FFmpeg).
"""
from __future__ import annotations

from dataclasses import dataclass

# Ограничения строки: на 1080 px при 68 px шрифте влезает примерно 28 символов, а больше четырёх слов
# на экране читать некогда — зритель не успевает.
MAX_WORDS = 4
MAX_CHARS = 28
MAX_SECONDS = 1.5
SENTENCE_ENDS = frozenset(".!?…")
GAP_BREAK_SECONDS = 1.5  # пауза в речи: там строка кончается даже без точки

PLAY_RES = (1080, 1920)  # .ass рисуется в системе координат кадра
SPEECH_SIZE = 68
HOOK_SIZE = 96
MARGIN_BOTTOM = 660  # нижняя граница safe zone: там подпись и кнопки TikTok
OUTLINE = 4
FILL_COLOUR = "&H0000E5FF"  # жёлтый: то, что уже сказано (BGR)
PENDING_COLOUR = "&H00FFFFFF"  # белый: то, что ещё не сказано
OUTLINE_COLOUR = "&H00000000"


@dataclass(frozen=True)
class Word:
    """Одно слово с временем звучания — в секундах от начала ролика."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Line:
    """Строка субтитров: 1–4 слова, которые видно одновременно."""

    words: tuple[Word, ...]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


def words_from_json3(payload: dict, *, limit: float | None = None) -> list[Word]:
    """Слова из формата `json3` (его отдаёт YouTube для авто-субтитров).

    В `json3` каждый сегмент — кусок фразы со своим временем; слова внутри него идут без времени,
    поэтому делим длительность сегмента поровну между словами. `limit` — обрезать всё после этой секунды
    (окно клипа): за его пределами субтитры не нужны.
    """
    events = payload.get("events") if isinstance(payload, dict) else None
    words: list[Word] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        start_ms = event.get("tStartMs")
        duration_ms = event.get("dDurationMs")
        segs = event.get("segs")
        if not isinstance(start_ms, (int, float)) or not isinstance(duration_ms, (int, float)):
            continue  # у служебных событий времени нет
        if not isinstance(segs, list) or not segs:
            continue
        pieces = [str(seg.get("utf8") or "") for seg in segs if isinstance(seg, dict)]
        text = "".join(pieces).replace("\n", " ").strip()
        if not text:
            continue
        tokens = text.split()
        if not tokens:
            continue
        start = start_ms / 1000
        if limit is not None and start > limit:
            break
        step = max(0.05, duration_ms / 1000 / len(tokens))
        for index, token in enumerate(tokens):
            words.append(Word(token, round(start + index * step, 3), round(start + (index + 1) * step, 3)))
    return _without_filler(words)


def _without_filler(words: list[Word]) -> list[Word]:
    """Убирает музыкальные вставки и пустые пометки вроде «[Музыка]»: их не читают, а место они занимают."""
    return [word for word in words if not (word.text.startswith("[") and word.text.endswith("]"))]


def lines_from_words(words: list[Word], *, limit: float | None = None) -> list[Line]:
    """Собирает слова в строки по 1–4 слова, не длиннее 28 символов и 1,5 секунды.

    Строка кончается раньше, если слово закрывает предложение (`.`, `?`, `!`, `…`) или после него
    в речи пауза: переносить фразу через паузу — значит показывать её, когда уже никто не говорит.
    """
    lines: list[Line] = []
    current: list[Word] = []
    for word in words:
        if limit is not None and word.start >= limit:
            break
        if current:
            too_long = len(current) >= MAX_WORDS
            too_wide = len(" ".join(item.text for item in current) + " " + word.text) > MAX_CHARS
            too_slow = word.end - current[0].start > MAX_SECONDS
            pause = word.start - current[-1].end > GAP_BREAK_SECONDS
            finished = current[-1].text.endswith(tuple(SENTENCE_ENDS))
            if too_long or too_wide or too_slow or pause or finished:
                lines.append(Line(tuple(current)))
                current = []
        current.append(word)
    if current:
        lines.append(Line(tuple(current)))
    return lines


def ass_time(seconds: float) -> str:
    """`75.5` → «0:01:15.50»: формат времени в `.ass` — часы:минуты:секунды.сотые."""
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{int(secs):02d}.{round((secs - int(secs)) * 100):02d}"


def ass_escape(text: str) -> str:
    """`{` и `}` в `.ass` — управляющие символы: обычный текст с ними развалит строку."""
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _karaoke(line: Line) -> str:
    """Текст строки с караоке-тегами: `\\k` показывает слово за его длительность в сотых секунды."""
    parts = []
    for word in line.words:
        centiseconds = max(1, round((word.end - word.start) * 100))
        parts.append(f"{{\\k{centiseconds}}}{ass_escape(word.text)}")
    return " ".join(parts)


def to_ass(lines: list[Line], *, font: str = "DejaVu Sans") -> str:
    """Готовый `.ass`: два стиля (речь и хук), караоке по словам, место — в нижней границе safe zone."""
    width, height = PLAY_RES
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Speech,{font},{SPEECH_SIZE},{FILL_COLOUR},{PENDING_COLOUR},{OUTLINE_COLOUR},&H64000000,-1,0,1,{OUTLINE},0,2,60,60,{MARGIN_BOTTOM},204
Style: Hook,{font},{HOOK_SIZE},{FILL_COLOUR},{PENDING_COLOUR},{OUTLINE_COLOUR},&H64000000,-1,0,1,{OUTLINE + 1},0,2,60,60,{MARGIN_BOTTOM + 120},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = "".join(
        f"Dialogue: 0,{ass_time(line.start)},{ass_time(line.end)},Speech,,0,0,0,,{_karaoke(line)}\n" for line in lines
    )
    return header + events


def lines_in_window(payload: dict, start: float, end: float) -> list[Line]:
    """Строки субтитров для окна клипа, со временем **от начала клипа**.

    Слова за пределами окна отбрасываются, время сдвигается на `start`: в графе FFmpeg ролик читается
    с `-ss start`, поэтому ноль клипа — это `start` источника.
    """
    words = [word for word in words_from_json3(payload, limit=end) if word.start >= start and word.end <= end]
    shifted = [Word(word.text, round(word.start - start, 3), round(word.end - start, 3)) for word in words]
    return lines_from_words(shifted, limit=end - start)


def subtitles_for_window(payload: dict, start: float, end: float, *, font: str = "DejaVu Sans") -> str:
    """Готовый `.ass` для окна клипа."""
    return to_ass(lines_in_window(payload, start, end), font=font)


def subtitles_for(payload: dict, *, limit: float | None = None, font: str = "DejaVu Sans") -> str:
    """Сразу готовый `.ass` из ответа площадки. Пустой список строк — пустой файл с одним заголовком."""
    return to_ass(lines_from_words(words_from_json3(payload, limit=limit), limit=limit), font=font)
