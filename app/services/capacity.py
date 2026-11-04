"""Защита общего сервера, когда бот открыт для всех: сколько задач ждёт в очереди и сколько осталось места.

Бот живёт на одной машине с другими сервисами, нарезка идёт в один поток, а скачанные ролики занимают диск.
Поэтому новая задача принимается, только пока очередь не заполнена и места хватает; иначе человек сразу получает
понятный отказ, а не ждёт часами или роняет соседей по серверу. Пределы настраиваются в `.env` (0 — не ограничивать).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

GB = 1024**3

MAX_QUEUE_ENV = "MAX_QUEUE"
MIN_FREE_ENV = "MIN_FREE_GB"
CACHE_LIMIT_ENV = "CACHE_LIMIT_GB"

DEFAULT_MAX_QUEUE = 6  # задач одновременно: идущая и ждущие
DEFAULT_MIN_FREE_GB = 5.0  # меньше — новые задачи не берём
DEFAULT_CACHE_LIMIT_GB = 12.0  # столько могут занимать скачанные источники

QUEUE_FULL = "Сейчас очередь заполнена — попробуйте через несколько минут."
DISK_LOW = "На сервере сейчас мало свободного места — попробуйте чуть позже."


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


def max_queue_from_env() -> int:
    """Сколько задач бот держит одновременно (идущая и ждущие); 0 — без предела."""
    return int(_number_from_env(MAX_QUEUE_ENV, DEFAULT_MAX_QUEUE))


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


def capacity_problem(unfinished: int, workspace: Path) -> str | None:
    """Почему сейчас нельзя принять ещё одну задачу (текст для человека) или `None`, если можно.

    `unfinished` — сколько задач уже идёт или ждёт. Не смогли измерить диск — не мешаем работать.
    """
    limit = max_queue_from_env()
    if limit and unfinished >= limit:
        return QUEUE_FULL
    needed = min_free_bytes_from_env()
    free = free_bytes(workspace) if needed else None
    if free is not None and free < needed:
        return DISK_LOW
    return None
