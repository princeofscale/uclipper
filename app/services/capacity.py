"""Защита общего сервера, когда бот открыт для всех: сколько нарезок идёт одновременно и сколько осталось места.

Бот живёт на одной машине с другими сервисами, нарезка грузит процессор целиком, а скачанные ролики занимают диск.
Поэтому по умолчанию бот ведёт **один** ролик за раз: пока он не готов (клипы не дошли до чата), остальным бот
отвечает «занят», и новая задача принимается, только когда хватает места. Человек сразу видит причину (красное меню),
а не ждёт часами и не роняет соседей по серверу. Пределы настраиваются в `.env` (0 — не ограничивать).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

GB = 1024**3

MAX_JOBS_ENV = "MAX_JOBS"
MIN_FREE_ENV = "MIN_FREE_GB"
CACHE_LIMIT_ENV = "CACHE_LIMIT_GB"

DEFAULT_MAX_JOBS = 1  # нарезок одновременно: один ролик за раз, остальным бот говорит «занят»
DEFAULT_MIN_FREE_GB = 5.0  # меньше — новые задачи не берём
DEFAULT_CACHE_LIMIT_GB = 12.0  # столько могут занимать скачанные источники


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
