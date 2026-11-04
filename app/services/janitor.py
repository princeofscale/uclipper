"""Уборка рабочей папки: скачанные источники, присланные файлы и недоделанные выдачи старше N дней.

Без неё на сервере кеш скачанных роликов растёт без предела. Баннеры, SFX и `users.json` не трогаются: чистятся
только `cache/`, `uploads/` и `outputs/`. Кроме возраста у кеша есть потолок по размеру (`CACHE_LIMIT_GB`): когда бот
открыт для всех, за три дня он мог бы занять весь диск общего сервера.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from app.config import Settings
from app.services.capacity import cache_limit_bytes_from_env

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 3.0
SWEEP_INTERVAL_SECONDS = 3600
PRUNE_ENV = "CLEANUP_DAYS"
CACHE_MIN_AGE_SECONDS = 6 * 3600  # потолок кеша не трогает совсем свежее: источник может быть нужен идущей задаче


def max_age_days_from_env() -> float:
    """`CLEANUP_DAYS` в `.env`; 0 или мусор отключают уборку (для 0) или возвращают значение по умолчанию."""
    raw = (os.getenv(PRUNE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        return max(0.0, float(raw.replace(",", ".")))
    except ValueError:
        log.warning("%s: «%s» — не число, беру %g дн.", PRUNE_ENV, raw, DEFAULT_MAX_AGE_DAYS)
        return DEFAULT_MAX_AGE_DAYS


def _newest_mtime(path: Path) -> float:
    """Самая свежая правка внутри папки (или сам файл): папку задачи удаляем, только когда она вся старая."""
    newest = path.stat().st_mtime
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    return newest


def _remove(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()


def _size(entry: Path) -> int:
    """Сколько байт занимает файл или папка со всем содержимым."""
    try:
        if entry.is_dir() and not entry.is_symlink():
            return sum(child.stat().st_size for child in entry.rglob("*") if child.is_file())
        return entry.stat().st_size
    except OSError:  # файл исчез по дороге — считаем, что его нет
        return 0


def prune_workspace(settings: Settings, max_age_days: float, *, now: float | None = None) -> int:
    """Удаляет старые записи в `cache/`, `uploads/`, `outputs/`. Возвращает, сколько записей убрано."""
    if max_age_days <= 0:
        return 0
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    removed = 0
    for root in (settings.cache_dir, settings.uploads_dir, settings.outputs_dir):
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            try:
                if _newest_mtime(entry) >= cutoff:
                    continue
                _remove(entry)
                removed += 1
            except OSError:
                log.warning("Не удалось убрать %s", entry, exc_info=True)
    if removed:
        log.info("Уборка: удалено записей — %d (старше %g дн.)", removed, max_age_days)
    return removed


def enforce_cache_limit(
    settings: Settings, limit_bytes: int, *, now: float | None = None, min_age_seconds: float = CACHE_MIN_AGE_SECONDS
) -> int:
    """Если кеш скачанных источников больше `limit_bytes`, убирает самые старые записи, пока он не влезет.

    Записи моложе `min_age_seconds` остаются при любом размере. Возвращает, сколько записей убрано; 0 в `limit_bytes` —
    без предела. Баннеры, загрузки и выдачи сюда не входят: их убирает `prune_workspace` по возрасту.
    """
    if limit_bytes <= 0 or not settings.cache_dir.is_dir():
        return 0
    entries = []
    for entry in settings.cache_dir.iterdir():
        try:
            entries.append((_newest_mtime(entry), _size(entry), entry))
        except OSError:
            continue
    total = sum(size for _, size, _ in entries)
    cutoff = (time.time() if now is None else now) - min_age_seconds
    removed = 0
    for mtime, size, entry in sorted(entries, key=lambda item: item[0]):  # от самой старой
        if total <= limit_bytes or mtime >= cutoff:
            break
        try:
            _remove(entry)
        except OSError:
            log.warning("Не удалось убрать %s", entry, exc_info=True)
            continue
        total -= size
        removed += 1
    if removed:
        log.info("Кеш больше %.1f ГБ: убрано самых старых записей — %d", limit_bytes / 1024**3, removed)
    return removed


async def sweep_forever(settings: Settings, max_age_days: float) -> None:
    """Фоновая уборка: сразу при старте и затем раз в час."""
    while True:
        try:
            await asyncio.to_thread(prune_workspace, settings, max_age_days)
            await asyncio.to_thread(enforce_cache_limit, settings, cache_limit_bytes_from_env())
        except Exception:  # noqa: BLE001 — уборка не должна ронять бота
            log.exception("Сбой уборки рабочей папки")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
