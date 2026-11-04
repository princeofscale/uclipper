"""Защита общего сервера, когда бот открыт для всех: сколько нарезок идёт одновременно, сколько осталось места
и сколько кусочков ролика качается разом.

Бот живёт на одной машине с другими сервисами, нарезка грузит процессор целиком, а скачанные ролики занимают диск.
Поэтому по умолчанию бот ведёт **один** ролик за раз: пока он не готов (клипы не дошли до чата), остальным бот
отвечает «занят», и новая задача принимается, только когда хватает места. Человек сразу видит причину (красное меню),
а не ждёт часами и не роняет соседей по серверу. Пределы настраиваются в `.env` (0 — не ограничивать).
"""
from __future__ import annotations

import logging
import math
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

GB = 1024**3

MAX_JOBS_ENV = "MAX_JOBS"
MIN_FREE_ENV = "MIN_FREE_GB"
CACHE_LIMIT_ENV = "CACHE_LIMIT_GB"
DOWNLOAD_THREADS_ENV = "DOWNLOAD_THREADS"
FFMPEG_THREADS_ENV = "FFMPEG_THREADS"
CGROUP_DIR = Path("/sys/fs/cgroup")

DEFAULT_MAX_JOBS = 1  # нарезок одновременно: один ролик за раз, остальным бот говорит «занят»
DEFAULT_MIN_FREE_GB = 5.0  # меньше — новые задачи не берём
DEFAULT_CACHE_LIMIT_GB = 12.0  # столько могут занимать скачанные источники
DEFAULT_DOWNLOAD_THREADS = 8  # кусочков ролика (фрагментов HLS/DASH) за раз: RuTube, 340 МБ: по одному ≈70 с, на 8 потоках ≈40 с
MAX_DOWNLOAD_THREADS = 16  # выше площадки отвечают отказами, а выигрыша нет


def _number_from_env(name: str, default: float) -> float:
    """Число из переменной окружения. Пусто или не число — `default`; отрицательное считается нулём («без предела»)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw.replace(",", ".")))
    except ValueError:
        log.warning("%s: «%s» — не число, беру %g", name, raw, default)
        return default


def max_jobs_from_env() -> int:
    """Сколько нарезок бот ведёт одновременно (идущие, ждущие запуска и досылающие результат); 0 — без предела."""
    return int(_number_from_env(MAX_JOBS_ENV, DEFAULT_MAX_JOBS))


def min_free_bytes_from_env() -> int:
    """Сколько места на диске должно оставаться, чтобы принять новую задачу; 0 — не проверять."""
    return int(_number_from_env(MIN_FREE_ENV, DEFAULT_MIN_FREE_GB) * GB)


def cache_limit_bytes_from_env() -> int:
    """Сколько байт может занимать кеш скачанных источников; 0 — без предела."""
    return int(_number_from_env(CACHE_LIMIT_ENV, DEFAULT_CACHE_LIMIT_GB) * GB)


def download_threads_from_env() -> int:
    """Сколько кусочков ролика качать одновременно (от 1 до `MAX_DOWNLOAD_THREADS`); 1 — по одному, как раньше."""
    wanted = int(_number_from_env(DOWNLOAD_THREADS_ENV, DEFAULT_DOWNLOAD_THREADS))
    return min(MAX_DOWNLOAD_THREADS, max(1, wanted))


def cpu_limit(cgroup_dir: Path = CGROUP_DIR) -> int | None:
    """Сколько ядер дозволено контейнеру (`cpus` в compose), округляя вверх; `None` — лимита нет или он не читается.

    Docker с cgroup v2 кладёт его в `cpu.max` как «квота период» (или «max период», если лимита нет).
    """
    try:
        quota, period = (cgroup_dir / "cpu.max").read_text().split()[:2]
        cores = int(quota) / int(period)
    except (OSError, ValueError, ZeroDivisionError):
        return None
    return max(1, math.ceil(cores)) if cores > 0 else None


def ffmpeg_threads() -> int | None:
    """Сколько потоков отдать ffmpeg; `None` — пусть выберет сам.

    ffmpeg видит все ядра хоста, а контейнеру разрешено меньше (на сервере — одно): лишние потоки не получают
    процессора, мешают друг другу и съедают четверть времени. Поэтому потоков столько, сколько разрешено контейнеру.
    `FFMPEG_THREADS` в `.env` важнее: число — столько потоков, 0 — не ограничивать.
    """
    wanted = _number_from_env(FFMPEG_THREADS_ENV, -1)
    if wanted < 0:
        return cpu_limit()
    return int(wanted) or None


def free_bytes(path: Path) -> int | None:
    """Свободное место на диске, где лежит `path`; `None`, если измерить не вышло."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def jobs_limit_reached(in_flight: int) -> bool:
    """Занято ли всё: `in_flight` нарезок уже идёт, и новую брать нельзя."""
    limit = max_jobs_from_env()
    return bool(limit) and in_flight >= limit


def disk_is_low(workspace: Path) -> bool:
    """Мало ли места на диске для новой задачи. Не смогли измерить — не мешаем работать."""
    needed = min_free_bytes_from_env()
    free = free_bytes(workspace) if needed else None
    return free is not None and free < needed
