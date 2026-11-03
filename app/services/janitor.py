"""Уборка рабочей папки: скачанные источники, присланные файлы и недоделанные выдачи старше N дней.

Без неё на сервере кеш YouTube растёт без предела. Баннеры, SFX и `users.json` не трогаются: чистятся
только `cache/`, `uploads/` и `outputs/`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from app.config import Settings

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 3.0
SWEEP_INTERVAL_SECONDS = 3600
PRUNE_ENV = "CLEANUP_DAYS"


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
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                log.warning("Не удалось убрать %s", entry, exc_info=True)
    if removed:
        log.info("Уборка: удалено записей — %d (старше %g дн.)", removed, max_age_days)
    return removed


async def sweep_forever(settings: Settings, max_age_days: float) -> None:
    """Фоновая уборка: сразу при старте и затем раз в час."""
    while True:
        try:
            await asyncio.to_thread(prune_workspace, settings, max_age_days)
        except Exception:  # noqa: BLE001 — уборка не должна ронять бота
            log.exception("Сбой уборки рабочей папки")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
