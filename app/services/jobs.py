"""Менеджер задач: очередь в один поток, прогресс, отмена и упаковка результата в ZIP.

Состояние задач хранится в памяти (готовые ролики — на диске, в `<outputs>/<job_id>/`). Бот сам решает,
когда удалить папку задачи после доставки файлов (`discard`).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.models import ClipResult, JobRequest, JobState, JobStatus, SlicingMode, SourceType, TrimInfo
from app.services.auto_trim import analyze_trim
from app.services.banner_manager import BannerError, ensure_sfx, resolve_banner
from app.services.downloader import (
    DownloadError,
    cached_source_path,
    download_video_cached,
    extract_video_id,
    resolve_upload,
)
from app.services.highlights import find_highlights
from app.services.video_processor import (
    ClipSchedule,
    MediaError,
    MediaInfo,
    RenderOptions,
    compute_clip_schedule,
    probe_media,
    render_clip,
    snap,
)

logger = logging.getLogger(__name__)

DOWNLOAD_END = 20.0  # скачивание занимает первые 20% шкалы
LOCAL_SOURCE_START = 2.0  # файл уже на диске (кеш/загрузка): рендер начинается почти с нуля
RENDER_END = 97.0  # рендер клипов — до 97%, остальное — упаковка в ZIP
MAX_LOG_LINES = 500
NOTIFY_SECONDS = 1.0  # как часто дёргать колбэк прогресса: чаще в Telegram всё равно не отправить

Notify = Callable[[JobState], None]


class JobCancelled(Exception):
    """Задачу отменил пользователь или бот выключается."""


class JobManager:
    """Принимает задачи и выполняет их по одной (FFmpeg и так занимает все ядра)."""

    def __init__(self, settings: Settings, *, max_workers: int = 1) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._jobs: dict[str, JobState] = {}
        self._archives: dict[str, Path] = {}
        self._cancelled: set[str] = set()
        self._stopping = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="clipper-job")

    # ---------- публичный интерфейс ----------

    def submit(self, request: JobRequest, *, on_update: Notify | None = None, on_clip: Notify | None = None) -> str:
        """Ставит задачу в очередь и сразу возвращает её id.

        `on_update` зовут при смене стадии и не чаще `NOTIFY_SECONDS` — по нему бот правит сообщение-прогресс.
        `on_clip` зовут, когда очередной клип готов (его можно сразу отправить пользователю).
        Колбэки выполняются в потоке задачи.
        """
        job_id = self._create()
        self._executor.submit(self._worker, request, job_id, on_update, on_clip)
        return job_id

    def run_sync(
        self,
        request: JobRequest,
        job_id: str | None = None,
        *,
        on_update: Notify | None = None,
        on_clip: Notify | None = None,
    ) -> JobState:
        """Выполняет задачу в текущем потоке. Не бросает исключений: ошибка попадает в `state.error`."""
        job_id = job_id or self._create()
        self._worker(request, job_id, on_update, on_clip)
        state = self.get(job_id)
        assert state is not None
        return state

    def get(self, job_id: str) -> JobState | None:
        """Копия состояния задачи (её можно спокойно менять) или None, если такой задачи нет."""
        with self._lock:
            state = self._jobs.get(job_id)
            return state.model_copy(deep=True) if state else None

    def cancel(self, job_id: str) -> bool:
        """Просит задачу остановиться на ближайшей проверке. True — задача была и ещё не закончилась."""
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
                return False
            self._cancelled.add(job_id)
            self._update(job_id, stage="Останавливаю")
        self._log(job_id, "Пользователь отменил задачу")
        return True

    def clip_path(self, job_id: str, clip: ClipResult) -> Path | None:
        """Файл готового клипа на диске."""
        path = self._settings.outputs_dir / job_id / clip.filename
        return path if path.is_file() else None

    def job_dir(self, job_id: str) -> Path:
        return self._settings.outputs_dir / job_id

    def zip_path(self, job_id: str) -> Path | None:
        """Архив с роликами — только у успешно завершённой задачи."""
        with self._lock:
            state, archive = self._jobs.get(job_id), self._archives.get(job_id)
        done = state is not None and state.status is JobStatus.DONE
        return archive if done and archive is not None and archive.is_file() else None

    def discard(self, job_id: str) -> None:
        """Удаляет папку задачи с готовыми роликами и архивом (после того как бот их отправил)."""
        directory = self.job_dir(job_id)
        with self._lock:
            self._archives.pop(job_id, None)
        if directory.is_dir():
            for path in sorted(directory.rglob("*"), reverse=True):  # сначала файлы, потом папки
                try:
                    path.unlink() if path.is_file() else path.rmdir()
                except OSError:  # файл мог уже исчезнуть — не повод падать
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass

    def shutdown(self) -> None:
        """Останавливает приём задач и прерывает выполняющуюся (на ближайшем сообщении о прогрессе)."""
        self._stopping.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ---------- состояние ----------

    def _create(self) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = JobState(id=job_id)
        self._log(job_id, "Задача создана")
        return job_id

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            state = self._jobs[job_id]
            for name, value in fields.items():
                setattr(state, name, value)

    def _progress(self, job_id: str, value: float) -> None:
        """Шкала только растёт: обратных прыжков пользователь не увидит."""
        with self._lock:
            state = self._jobs[job_id]
            state.progress = max(state.progress, min(100.0, value))

    def _log(self, job_id: str, message: str) -> None:
        with self._lock:
            lines = self._jobs[job_id].log
            lines.append(f"[{time.strftime('%H:%M:%S')}] {message}")
            del lines[:-MAX_LOG_LINES]

    def _fail(self, job_id: str, message: str) -> None:
        self._update(job_id, status=JobStatus.ERROR, stage="Ошибка", error=message)
        self._log(job_id, f"Ошибка: {message.splitlines()[0] if message else 'неизвестная'}")

    def _check_not_stopping(self, job_id: str | None = None) -> None:
        if self._stopping.is_set():
            raise JobCancelled
        if job_id is not None:
            with self._lock:
                if job_id in self._cancelled:
                    raise JobCancelled

    # ---------- сама задача ----------

    def _worker(
        self,
        request: JobRequest,
        job_id: str,
        on_update: Notify | None,
        on_clip: Notify | None,
    ) -> None:
        """Обёртка задачи: превращает любое исключение в состояние ошибки и не роняет поток."""
        notifier = _Notifier(self, job_id, on_update)
        try:
            self._run(request, job_id, notifier, on_clip)
        except JobCancelled:
            if self._stopping.is_set():
                self._update(job_id, status=JobStatus.ERROR, stage="Ошибка", error="Задача остановлена: бот выключается.")
            else:
                self._update(job_id, status=JobStatus.CANCELLED, stage="Отменено")
                self._log(job_id, "Задача отменена")
            self._cleanup_partial(job_id)
        except (DownloadError, BannerError, MediaError) as error:
            self._fail(job_id, str(error))
        except Exception as error:  # noqa: BLE001 — задача не должна ронять поток-исполнитель
            logger.exception("Непредвиденная ошибка в задаче %s", job_id)
            self._fail(job_id, f"Непредвиденная ошибка: {error}")
        notifier.flush(force=True)

    def _cleanup_partial(self, job_id: str) -> None:
        """Убирает недоделанные файлы отменённой задачи."""
        directory = self.job_dir(job_id)
        if not directory.is_dir():
            return
        for path in directory.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _run(self, request: JobRequest, job_id: str, notifier: "_Notifier", on_clip: Notify | None) -> None:
        settings = self._settings
        self._update(job_id, status=JobStatus.RUNNING, stage="Подготовка", progress=0.0)
        notifier.touch()

        # Сначала дешёвые проверки: плохой баннер не должен всплыть после долгого скачивания.
        wants_banner = request.banner_count > 0
        banner = resolve_banner(request.banner_id or "", settings.banners_dir) if wants_banner else None
        static = resolve_banner(request.static_banner_id, settings.banners_dir) if request.static_banner_id else None
        sfx = ensure_sfx(settings.sfx_dir) if wants_banner and request.enable_sfx else None

        source, downloaded = self._obtain_source(request, job_id, notifier)
        render_start = DOWNLOAD_END if downloaded else LOCAL_SOURCE_START
        self._progress(job_id, render_start)
        notifier.touch()

        self._update(job_id, stage="Анализ видео")
        info = probe_media(source)
        sound = "со звуком" if info.has_audio else "без звука"
        self._log(job_id, f"Источник: {info.width}×{info.height}, {info.duration:.1f} с, {sound}")
        start_offset, end_offset = self._trim(request, job_id, source, info)
        try:
            schedule = self._schedule(request, job_id, source, info, start_offset, end_offset)
        except ValueError as error:
            raise MediaError(str(error)) from error
        for warning in schedule.warnings:
            self._log(job_id, f"Внимание: {warning}")
        notifier.touch()

        job_dir = settings.outputs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        options = RenderOptions(
            frame_mode=request.frame_mode,
            banner_mode=request.banner_mode,
            banner_count=request.banner_count,
            banner_duration=request.banner_duration,
            banner_position=request.banner_position,
            banner_scale=request.banner_scale,
            banner_sound=request.banner_sound,
            enable_sfx=request.enable_sfx,
            static_position=request.static_position,
            static_scale=request.static_scale,
        )
        clips = self._render_all(
            job_id, job_dir, source, info, schedule.starts, schedule.clip_duration, options,
            banner, static, sfx, render_start, notifier, on_clip,
        )  # fmt: skip

        self._update(job_id, stage="Упаковка архива")
        self._progress(job_id, RENDER_END)
        notifier.touch()
        archive = job_dir / f"clips_{job_id}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:  # mp4 уже сжаты — просто складываем
            for clip in clips:
                bundle.write(job_dir / clip.filename, arcname=clip.filename)
        with self._lock:
            self._archives[job_id] = archive
        self._update(job_id, status=JobStatus.DONE, stage="Готово", progress=100.0, archive_name=archive.name)
        self._log(job_id, f"Готово: роликов — {len(clips)}, архив {archive.name}")
        notifier.touch()

    def _schedule(
        self, request: JobRequest, job_id: str, source: Path, info: MediaInfo, start_offset: float, end_offset: float
    ) -> ClipSchedule:
        """Где в источнике лежат клипы. Режим «яркие моменты» ищет их по звуку, а если не вышло — раскладывает ровно."""
        highlights = request.slicing_mode is SlicingMode.HIGHLIGHTS
        base = compute_clip_schedule(
            info.duration,
            start_offset,
            request.num_clips,
            request.clip_duration,
            SlicingMode.EVENLY_SPACED if highlights else request.slicing_mode,
            end_offset,
        )
        if not highlights:
            return base
        self._update(job_id, stage="Ищу яркие моменты")
        self._check_not_stopping(job_id)
        starts: list[float] = []
        if info.has_audio:
            starts = find_highlights(
                source, start_offset, info.duration - end_offset, request.num_clips, base.clip_duration
            )
        if not starts:
            warning = "Яркие моменты найти не вышло (в ролике мало звука) — клипы распределены равномерно."
            self._log(job_id, f"Внимание: {warning}")
            return ClipSchedule(base.starts, base.clip_duration, base.slicing_mode, [*base.warnings, warning])
        starts = [snap(second) for second in starts]
        self._log(job_id, "Яркие моменты: " + ", ".join(f"{second:.0f} с" for second in starts))
        kept = [warning for warning in base.warnings if "перекрыв" not in warning]
        return ClipSchedule(starts, base.clip_duration, SlicingMode.HIGHLIGHTS, kept)

    def _trim(self, request: JobRequest, job_id: str, source: Path, info: MediaInfo) -> tuple[float, float]:
        """Сколько секунд отрезать с начала и с конца: ищет программа (`auto_trim`) или это задано вручную."""
        if not request.auto_trim:
            start, end = request.start_offset, request.end_offset
            if start or end:
                self._update(job_id, trim=TrimInfo(auto=False, start=start, end=round(info.duration - end, 3)))
                self._log(job_id, f"Смещения вручную: убираю {start:g} с в начале и {end:g} с в конце")
            return start, end

        self._update(job_id, stage="Поиск начала и конца")
        found = analyze_trim(source, info)
        self._update(job_id, trim=TrimInfo(auto=True, start=found.start, end=found.end, head=found.head, tail=found.tail))
        if found.head is None and found.tail is None:
            self._log(job_id, "Автообрезка: лишнего в начале и в конце не нашла — беру ролик целиком")
        else:
            parts = [f"начало с {found.start:.1f} с ({found.head})" if found.head else None,
                     f"конец на {found.end:.1f} с ({found.tail})" if found.tail else None]  # fmt: skip
            self._log(job_id, "Автообрезка: " + ", ".join(part for part in parts if part))
        return found.start_offset, found.end_offset

    def _obtain_source(self, request: JobRequest, job_id: str, notifier: "_Notifier") -> tuple[Path, bool]:
        """Путь к исходному видео и признак «его пришлось скачивать»."""
        settings = self._settings
        self._check_not_stopping(job_id)
        if request.source_type is SourceType.UPLOAD:
            self._update(job_id, stage="Загруженный файл")
            path = resolve_upload(request.upload_id or "", settings.uploads_dir)
            self._log(job_id, "Использую присланный видеофайл")
            notifier.touch()
            return path, False

        url = (request.url or "").strip()
        video_id = extract_video_id(url)
        if video_id and cached_source_path(video_id, settings.cache_dir):
            self._update(job_id, stage="Видео из кеша")
            self._log(job_id, "Видео уже есть в кеше — повторно не скачиваю")
            notifier.touch()
            return download_video_cached(url, request.max_height, cache_dir=settings.cache_dir), False

        self._update(job_id, stage="Скачивание видео")
        self._log(job_id, f"Скачиваю видео с YouTube (до {request.max_height}p)…")
        notifier.touch()

        def on_download(fraction: float) -> None:
            self._check_not_stopping(job_id)
            self._progress(job_id, fraction * DOWNLOAD_END)
            notifier.touch()

        path = download_video_cached(url, request.max_height, cache_dir=settings.cache_dir, on_progress=on_download)
        self._log(job_id, "Видео скачано")
        notifier.touch()
        return path, True

    def _render_all(
        self,
        job_id: str,
        job_dir: Path,
        source: Path,
        info: MediaInfo,
        starts: list[float],
        clip_duration: float,
        options: RenderOptions,
        banner: Path | None,
        static: Path | None,
        sfx: Path | None,
        render_start: float,
        notifier: "_Notifier",
        on_clip: Notify | None,
    ) -> list[ClipResult]:
        total = len(starts)
        self._update(job_id, total_clips=total)
        self._log(job_id, f"Нарезаю клипов: {total}, чистая длительность каждого — {clip_duration:g} с")
        clips: list[ClipResult] = []
        for position, start in enumerate(starts):
            self._check_not_stopping(job_id)
            number = position + 1
            filename = f"clip_{number:02d}.mp4"
            self._update(job_id, stage=f"Рендер клипа {number} из {total}")
            self._log(job_id, f"Клип {number} из {total}: источник {start:.1f}–{start + clip_duration:.1f} с…")
            notifier.touch()

            def on_progress(fraction: float, position: int = position) -> None:
                self._check_not_stopping(job_id)
                self._progress(job_id, render_start + (RENDER_END - render_start) * (position + fraction) / total)
                notifier.touch()

            timeline = render_clip(
                source, job_dir / filename, start, clip_duration, options,
                info=info, banner=banner, static=static, sfx=sfx, on_progress=on_progress,
            )  # fmt: skip
            clip = ClipResult(
                index=number,
                filename=filename,
                source_start=round(start, 3),
                source_end=round(start + clip_duration, 3),
                duration=round(timeline.total_duration, 3),
                banner_points=[round(point, 3) for point in timeline.freeze_points],
                banner_windows=[(round(begin, 3), round(end, 3)) for begin, end in timeline.windows],
            )
            clips.append(clip)
            with self._lock:  # клип виден сразу, не дожидаясь остальных
                self._jobs[job_id].clips.append(clip)
            self._log(job_id, f"Клип {number} готов: {clip.duration:.1f} с")
            notifier.touch()
            if on_clip is not None:
                on_clip(self.get(job_id))
        return clips


class _Notifier:
    """Зовёт колбэк прогресса не чаще `NOTIFY_SECONDS` и всегда — при смене стадии или статуса."""

    def __init__(self, manager: JobManager, job_id: str, callback: Notify | None) -> None:
        self._manager = manager
        self._job_id = job_id
        self._callback = callback
        self._last_call = 0.0
        self._last_stage: str | None = None
        self._last_status: str | None = None

    def touch(self) -> None:
        """Сообщает о прогрессе, если прошла секунда или сменились стадия/статус."""
        if self._callback is None:
            return
        now = time.monotonic()
        state = self._manager.get(self._job_id)
        if state is None:
            return
        changed = state.stage != self._last_stage or state.status.value != self._last_status
        if not changed and now - self._last_call < NOTIFY_SECONDS:
            return
        self.flush(force=changed)

    def flush(self, *, force: bool = False) -> None:
        if self._callback is None:
            return
        state = self._manager.get(self._job_id)
        if state is None:
            return
        if not force and time.monotonic() - self._last_call < NOTIFY_SECONDS:
            return
        self._last_call = time.monotonic()
        self._last_stage = state.stage
        self._last_status = state.status.value
        self._callback(state)
