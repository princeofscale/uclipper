"""Яркие моменты по звуку: где говорят громче и живее, там обычно и самое интересное.

Это не нейросеть, а честная эвристика по громкости. Ролик делится на столько равных частей, сколько нужно
клипов, и из каждой берётся окно, где звук одновременно громкий и «живой» (смех, споры, акценты, музыка
в нужный момент), а тишины мало. Так клипы не сбиваются в кучу и не берут паузы.

`loudness_profile` читает звук одним проходом FFmpeg (4 кГц, моно — для громкости этого хватает), а
`pick_windows` — чистая функция над списком громкостей, поэтому проверяется без видео.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 4000  # Гц: для громкости речи достаточно, а поток в 11 раз легче 44,1 кГц
CHUNK_SECONDS = 60  # звук читаем по минуте, чтобы часовой ролик не раздувал память
SILENT_BELOW_MEDIAN_DB = 15.0  # секунда тише медианы на столько дБ считается паузой
LIVELY_WEIGHT = 0.7  # вес «живости» (разброса громкости) рядом со средней громкостью
SILENCE_PENALTY = 12.0  # штраф окну за долю тишины
FLOOR_DB = -70.0


def loudness_profile(source: Path, start: float, end: float, *, timeout: float = 600.0) -> list[float]:
    """Громкость каждой секунды отрезка `[start, end]` источника, дБ (RMS). Пусто, если звука нет или FFmpeg не справился."""
    length = end - start
    if length < 1:
        return []
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]  # fmt: skip
    levels: list[float] = []
    carry = np.empty(0, dtype=np.float32)
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            assert process.stdout is not None
            while True:
                raw = process.stdout.read(SAMPLE_RATE * 2 * CHUNK_SECONDS)
                if not raw:
                    break
                samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
                samples = np.concatenate([carry, samples])
                whole = len(samples) // SAMPLE_RATE * SAMPLE_RATE
                carry = samples[whole:]
                if whole:
                    seconds = samples[:whole].reshape(-1, SAMPLE_RATE)
                    rms = np.sqrt(np.mean(np.square(seconds), axis=1))
                    levels.extend(np.maximum(20 * np.log10(np.maximum(rms, 1e-7)), FLOOR_DB).tolist())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                return []
    except (OSError, ValueError):
        log.warning("Не получилось прочитать звук для поиска ярких моментов", exc_info=True)
        return []
    return levels


def window_scores(levels: list[float], window: int) -> np.ndarray:
    """Оценка каждого окна из `window` секунд: громкость + живость − штраф за тишину. Индекс — начало окна."""
    data = np.asarray(levels, dtype=np.float64)
    count = len(data) - window + 1
    if count <= 0:
        return np.empty(0)
    silent = (data < np.median(data) - SILENT_BELOW_MEDIAN_DB).astype(np.float64)
    prefix = np.concatenate([[0.0], np.cumsum(data)])
    prefix_sq = np.concatenate([[0.0], np.cumsum(data * data)])
    prefix_silent = np.concatenate([[0.0], np.cumsum(silent)])
    mean = (prefix[window:] - prefix[:-window]) / window
    spread = np.sqrt(np.maximum((prefix_sq[window:] - prefix_sq[:-window]) / window - mean * mean, 0.0))
    quiet = (prefix_silent[window:] - prefix_silent[:-window]) / window
    return mean + LIVELY_WEIGHT * spread - SILENCE_PENALTY * quiet


def pick_windows(levels: list[float], num_clips: int, clip_seconds: float) -> list[int]:
    """Секунда начала лучшего окна в каждой из `num_clips` равных частей ролика (по возрастанию).

    Окна не пересекаются. Пустой список — окна не помещаются в запись: звука мало, пусть решает вызывающий.
    """
    window = max(1, round(clip_seconds))
    if num_clips < 1 or num_clips * window > len(levels):
        return []
    scores = window_scores(levels, window)
    last_start = len(scores) - 1  # самый поздний допустимый старт
    starts: list[int] = []
    previous_end = 0
    for index in range(num_clips):
        lo = max(index * len(scores) // num_clips, previous_end)
        hi = min((index + 1) * len(scores) // num_clips - 1, last_start - (num_clips - 1 - index) * window)
        lo = min(lo, last_start - (num_clips - 1 - index) * window)  # впереди должно остаться место остальным
        hi = max(hi, lo)
        best = lo + int(np.argmax(scores[lo : hi + 1]))
        starts.append(best)
        previous_end = best + window
    return starts


def find_highlights(source: Path, start: float, end: float, num_clips: int, clip_seconds: float) -> list[float]:
    """Времена начала ярких клипов в источнике, с. Пусто — анализ не вышел (нет звука, слишком коротко)."""
    levels = loudness_profile(source, start, end)
    picked = pick_windows(levels, num_clips, clip_seconds)
    return [round(start + second, 3) for second in picked]
