"""Хранилище настроек пользователей: небольшой JSON-файл в рабочей папке.

Нужно, чтобы настройки не терялись при перезапуске бота и при следующем ролике.

Хранятся только id и настройки (никаких имён и переписки). Запись атомарная: сначала во временный
файл, потом `replace` — оборванная запись не испортит базу.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app.models import ClipSettings

log = logging.getLogger(__name__)

FILE_NAME = "users.json"


class UserStore:
    """Настройки по user_id. Ошибки чтения/записи не роняют бота — просто остаётся память процесса."""

    def __init__(self, workspace_dir: Path) -> None:
        self._path = workspace_dir / FILE_NAME
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ---------- загрузка/сохранение ----------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            log.warning("Не удалось прочитать %s — начну с пустого списка настроек.", self._path)
            return
        if isinstance(raw, dict):
            self._data = {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def save(self) -> None:
        """Сбрасывает накопленные изменения на диск (атомарно)."""
        with self._lock:
            if not self._dirty:
                return
            payload = json.dumps(self._data, ensure_ascii=False, indent=0, sort_keys=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            staging = self._path.with_suffix(".tmp")
            try:
                staging.write_text(payload, "utf-8")
                staging.replace(self._path)
                self._dirty = False
            except OSError:
                log.warning("Не удалось сохранить настройки пользователей в %s.", self._path, exc_info=True)
                staging.unlink(missing_ok=True)

    # ---------- настройки ----------

    def settings(self, user_id: int) -> ClipSettings:
        """Настройки пользователя или значения по умолчанию."""
        with self._lock:
            raw = self._data.get(str(user_id), {})
        try:
            return ClipSettings(**raw)
        except (TypeError, ValueError):  # старый/битый формат — не роняем бота
            log.warning("Настройки пользователя %s не подошли — беру значения по умолчанию.", user_id)
            return ClipSettings()

    def remember_settings(self, user_id: int, settings: ClipSettings) -> None:
        with self._lock:
            entry = self._data.setdefault(str(user_id), {})
            entry.update(settings.model_dump(mode="json"))
            self._dirty = True

    # ---------- статистика ----------

    def count_job(self, user_id: int, clips: int) -> None:
        """Учитывает выполненную задачу: общее число роликов и задач."""
        with self._lock:
            entry = self._data.setdefault(str(user_id), {})
            entry["clips_total"] = int(entry.get("clips_total", 0)) + max(0, clips)
            entry["jobs_total"] = int(entry.get("jobs_total", 0)) + 1
            self._dirty = True

    def stats(self, user_id: int) -> tuple[int, int]:
        """`(роликов нарезано, задач выполнено)`."""
        with self._lock:
            entry = self._data.get(str(user_id), {})
            return int(entry.get("clips_total", 0)), int(entry.get("jobs_total", 0))
