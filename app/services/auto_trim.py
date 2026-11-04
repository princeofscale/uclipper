"""Автообрезка: где на самом деле начинается и кончается ролик — без чёрного экрана, тишины и титров.

Это эвристика, а не нейросеть: никаких моделей и весов, всё считается обычными средствами FFmpeg и numpy и
объясняется человеческим языком («чёрный экран», «титры»). Смотрит только края видео — первые секунды и
последние минуты; середина не трогается.

* **Начало:** чёрные кадры (`blackdetect`) и тишина (`silencedetect`), которые начинаются сразу с нуля.
* **Конец:** то же самое в конце, плюс **титры** — по ключевым кадрам: тёмный фон с мелким светлым текстом, без
  полутонов (обычная тёмная сцена текста не содержит и не считается). Титры короче 12 секунд не трогаем.
* Если после обрезки остаётся меньше половины ролика, анализу не верим и ничего не режем.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.services.video_processor import MediaError, MediaInfo, probe_media

MIN_ANALYSED_SECONDS = 10.0  # совсем короткий ролик не анализируем
HEAD_SCAN = 40.0  # сколько секунд с начала смотрим на чёрный экран и тишину
TAIL_SCAN = 60.0  # и с конца
# Чёрный экран ищется разбором каждого кадра — это самая дорогая часть анализа. Обычный ролик не начинается и не
# кончается чёрным, поэтому сначала смотрим коротко и расширяем окно до HEAD_SCAN/TAIL_SCAN, только если чёрное
# упёрлось в границу окна.
HEAD_BLACK_FIRST = 8.0
TAIL_BLACK_FIRST = 12.0
CREDITS_SCAN = 600.0  # на сколько секунд от конца ищем титры...
CREDITS_SCAN_SHARE = 0.50  # ...но не больше этой доли ролика (больше половины всё равно не режем)
MIN_CREDITS = 12.0  # короче — это не титры
MIN_USABLE_SHARE = 0.5  # после обрезки должно остаться не меньше половины ролика
EDGE_TOLERANCE = 0.5  # интервал «у края», если начинается не позже / кончается не раньше, чем за столько секунд
SAMPLE_SIZE = (256, 144)  # кадры для поиска титров уменьшаются до этого размера, серые

_BLACK = re.compile(r"black_start:\s*(-?[\d.]+)\s+black_end:\s*(-?[\d.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_FRAME_TIME = re.compile(r"pts_time:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class TrimSuggestion:
    duration: float  # длина источника, с
    start: float  # с какой секунды начинается «настоящее» видео
    end: float  # на какой секунде оно заканчивается
    head: str | None  # что убрано в начале (по-русски) или None
    tail: str | None  # что убрано в конце

    @property
    def start_offset(self) -> float:
        return self.start

    @property
    def end_offset(self) -> float:
        return self.duration - self.end


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *args], capture_output=True)
    except FileNotFoundError as error:
        raise MediaError("Не найден ffmpeg — установите FFmpeg и добавьте его в PATH.") from error


def _stderr(result: subprocess.CompletedProcess) -> str:
    return result.stderr.decode("utf-8", errors="replace")


def _black_intervals(path: Path, start: float, length: float) -> list[tuple[float, float]]:
    """Участки чёрного экрана внутри `[start, start+length]` (абсолютные секунды)."""
    result = _ffmpeg([
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path),
        "-an", "-vf", "scale=160:-2,blackdetect=d=0.2:pix_th=0.10:pic_th=0.98", "-f", "null", "-",
    ])  # fmt: skip
    return [(start + float(a), start + float(b)) for a, b in _BLACK.findall(_stderr(result))]


def _black_at_edge(path: Path, duration: float, *, head: bool) -> list[tuple[float, float]]:
    """Участки чёрного экрана у начала (`head`) или у конца ролика; окно растёт, пока чёрное упирается в его границу."""
    limit = min(HEAD_SCAN if head else TAIL_SCAN, duration)
    first = min(HEAD_BLACK_FIRST if head else TAIL_BLACK_FIRST, limit)
    intervals: list[tuple[float, float]] = []
    for length in dict.fromkeys((first, limit)):
        start = 0.0 if head else duration - length
        intervals = _black_intervals(path, start, length)
        if head:  # чёрное идёт с самого начала и не кончилось до конца окна — дальше может быть ещё
            runs_on = any(begin <= EDGE_TOLERANCE and stop >= length - EDGE_TOLERANCE for begin, stop in intervals)
        else:
            runs_on = any(stop >= duration - EDGE_TOLERANCE and begin <= start + EDGE_TOLERANCE for begin, stop in intervals)
        if not runs_on:
            break
    return intervals


def _silence_intervals(path: Path, start: float, length: float) -> list[tuple[float, float]]:
    """Участки тишины (тише −50 дБ, не короче секунды); тишина, дошедшая до конца окна, закрывается его концом."""
    result = _ffmpeg([
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path),
        "-vn", "-af", "silencedetect=n=-50dB:d=1.0", "-f", "null", "-",
    ])  # fmt: skip
    intervals: list[tuple[float, float]] = []
    opened: float | None = None
    for line in _stderr(result).splitlines():
        if (match := _SILENCE_START.search(line)) is not None:
            opened = start + float(match.group(1))
        elif (match := _SILENCE_END.search(line)) is not None and opened is not None:
            intervals.append((opened, start + float(match.group(1))))
            opened = None
    if opened is not None:
        intervals.append((opened, start + length))
    return intervals


def _sample_keyframes(path: Path, start: float, length: float) -> list[tuple[float, np.ndarray]]:
    """Серые уменьшенные ключевые кадры окна, не чаще одного в секунду: `[(секунда, массив высота×ширина)]`.

    Декодируются только ключевые кадры (`-skip_frame nokey`), поэтому даже 10 минут FullHD читаются за секунды.
    """
    width, height = SAMPLE_SIZE
    result = _ffmpeg([
        "-skip_frame", "nokey", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path), "-an", "-sn",
        "-vf", f"select=isnan(prev_selected_t)+gte(t-prev_selected_t\\,1),scale={width}:{height},format=gray,showinfo",
        "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
    ])  # fmt: skip
    times = [start + float(value) for value in _FRAME_TIME.findall(_stderr(result))]
    frames = np.frombuffer(result.stdout, dtype=np.uint8)
    count = min(len(times), frames.size // (width * height))
    return list(zip(times[:count], frames[: count * width * height].reshape(count, height, width)))


def _classify(frame: np.ndarray) -> str:
    """`black` — чёрный кадр; `text` — тёмный фон со светлым мелким текстом (титры); `other` — всё остальное."""
    if frame.mean() <= 18 and (frame >= 60).mean() < 0.005:
        return "black"
    dark = float((frame <= 40).mean())
    bright = float((frame >= 140).mean())
    midtones = 1.0 - dark - bright
    if dark >= 0.80 and 0.003 <= bright <= 0.25 and midtones <= 0.12:
        return "text"
    return "other"


def _credits_start(samples: list[tuple[float, np.ndarray]], duration: float) -> float | None:
    """Секунда, с которой до самого конца идут только титры и чёрные кадры; `None`, если такого хвоста нет."""
    kinds = [(moment, _classify(frame)) for moment, frame in samples]
    boundary: float | None = None
    misses = 0
    for moment, kind in reversed(kinds):
        if kind == "other":
            misses += 1
            if misses > 1:  # один «чужой» кадр среди титров (вспышка, логотип) прощаем, два подряд — конец титров
                break
            continue
        boundary, misses = moment, 0
    if boundary is None or duration - boundary < MIN_CREDITS:
        return None
    run = [kind for moment, kind in kinds if moment >= boundary]
    if run.count("text") < 3 or run.count("text") / len(run) < 0.4:
        return None  # в хвосте почти один чёрный экран: его уже режет blackdetect, а титрами это не назвать
    return boundary


def _label(reasons: list[str]) -> str | None:
    return ", ".join(dict.fromkeys(reasons)) if reasons else None


def analyze_trim(path: Path, info: MediaInfo | None = None) -> TrimSuggestion:
    """Предлагает, с какой по какую секунду брать видео. Режет только края; сомнительное оставляет как есть.

    Результат запоминается для файла (путь, размер, время изменения): страница и задача спрашивают одно и то же.
    """
    stat = path.stat()
    return _analyze(str(path), stat.st_size, stat.st_mtime_ns, info)


@lru_cache(maxsize=16)
def _analyze(path_text: str, size: int, mtime_ns: int, info: MediaInfo | None) -> TrimSuggestion:
    path = Path(path_text)
    info = info or probe_media(path)
    duration = info.duration
    untouched = TrimSuggestion(duration=duration, start=0.0, end=duration, head=None, tail=None)
    if duration < MIN_ANALYSED_SECONDS:
        return untouched

    head_scan = min(HEAD_SCAN, duration)
    tail_scan = min(TAIL_SCAN, duration)
    black_head = _black_at_edge(path, duration, head=True)
    black_tail = _black_at_edge(path, duration, head=False)
    silence_head = _silence_intervals(path, 0.0, head_scan) if info.has_audio else []
    silence_tail = _silence_intervals(path, duration - tail_scan, tail_scan) if info.has_audio else []

    # --- начало: чёрный экран и тишина, которые идут сразу с нуля ---
    start, head_reasons = 0.0, []
    for label, intervals in (("чёрный экран", black_head), ("тишина", silence_head)):
        edge = next((end for begin, end in intervals if begin <= EDGE_TOLERANCE), None)
        if edge is not None:
            start = max(start, edge)
            head_reasons.append(label)

    # --- конец: чёрный экран и тишина до самого конца, затем титры ---
    end, tail_reasons = duration, []
    for label, intervals in (("чёрный экран", black_tail), ("тишина", silence_tail)):
        edge = next((begin for begin, stop in intervals if stop >= duration - EDGE_TOLERANCE), None)
        if edge is not None:
            end = min(end, edge)
            tail_reasons.append(label)

    credits_scan = min(CREDITS_SCAN, duration * CREDITS_SCAN_SHARE)
    credits = _credits_start(_sample_keyframes(path, duration - credits_scan, credits_scan), duration)
    if credits is not None and credits < end:
        end = credits
        tail_reasons.insert(0, "титры")

    if end - start < duration * MIN_USABLE_SHARE:  # анализу не верим: ролик целиком «чёрный» или «титры» — это ошибка
        return untouched
    return TrimSuggestion(
        duration=duration,
        start=round(start, 2),
        end=round(end, 2),
        head=_label(head_reasons) if start > 0 else None,
        tail=_label(tail_reasons) if end < duration else None,
    )
