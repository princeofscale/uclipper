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
from app.services.audio import normalize_file as normalize_loudness
from app.services.banner_manager import BannerError, ensure_sfx, resolve_banner
from app.services.capacity import ffmpeg_threads, max_jobs_from_env, normalize_audio_from_env
from app.services.downloader import DownloadError, cached_source_path, download_video_cached, resolve_upload
from app.services.highlights import edge_reserve, find_highlights
from app.services.platforms import identify
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


def _duration(seconds: float) -> str:
    """«7 с», «3 мин 20 с» — для строк лога задачи: секунды без десятых на длинных этапах не читаются."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} с"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"


class JobCancelled(Exception):
    """Задачу отменил пользователь или бот выключается."""


class JobManager:
    """Принимает задачи и выполняет их параллельно — столько сразу, сколько разрешено в `MAX_JOBS`.

    Потоков ffmpeg каждой задаче достаётся `ядра / число идущих нарезок`, поэтому две нарезки на двух ядрах
    не отнимают друг у друга процессор, а просто делят его.
    """

    def __init__(self, settings: Settings, *, max_workers: int | None = None) -> None:
        self._settings = settings
        self._max_workers = max(1, max_workers if max_workers is not None else max_jobs_from_env())
        self._lock = threading.RLock()
        self._jobs: dict[str, JobState] = {}
        self._archives: dict[str, Path] = {}
        self._cancelled: set[str] = set()
        self._normalize_audio = normalize_audio_from_env()
        self._stopping = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="clipper-job")

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
        """Отменяет задачу. True — задача была и ещё не закончилась.

        Ждущая в очереди отменяется сразу и до выполнения не доходит; идущая останавливается на ближайшей проверке.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
                return False
            self._cancelled.add(job_id)
            if state.status is JobStatus.QUEUED:
                self._update(job_id, status=JobStatus.CANCELLED, stage="Отменено")
            else:
                self._update(job_id, stage="Останавливаю")
        self._log(job_id, "Пользователь отменил задачу")
        return True

    def ahead_of(self, job_id: str) -> int:
        """Сколько задач в очереди впереди этой: только ждущие, без идущих (они уже занимают свои места). 0 — её очередь."""
        with self._lock:
            ahead = 0
            for other_id, state in self._jobs.items():  # словарь хранит задачи в порядке создания
                if other_id == job_id:
                    return ahead
                if state.status is JobStatus.QUEUED:
                    ahead += 1
            return 0

    def running_count(self) -> int:
        """Сколько нарезок идёт прямо сейчас: по нему делится процессор между задачами."""
        with self._lock:
            return sum(1 for state in self._jobs.values() if state.status is JobStatus.RUNNING)

    def max_parallel(self) -> int:
        """Сколько нарезок бот ведёт одновременно."""
        return self._max_workers

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
        """Забывает задачу и удаляет её папку с готовыми роликами и архивом (после того как бот их отправил)."""
        directory = self.job_dir(job_id)
        with self._lock:
            self._archives.pop(job_id, None)
            self._cancelled.discard(job_id)
            self._jobs.pop(job_id, None)  # иначе у бота, открытого для всех, память росла бы с каждой задачей
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
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state.status is JobStatus.CANCELLED:
                return  # отменена, пока ждала очереди (или забыта): выполнять нечего
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
        self._update(job_id, status=JobStatus.RUNNING, stage="Подготовка", progress=0.0, preview=request.preview)
        notifier.touch()

        # Сначала дешёвые проверки: плохой баннер не должен всплыть после долгого скачивания.
        wants_banner = request.banner_count > 0
        banner = resolve_banner(request.banner_id or "", settings.banners_dir) if wants_banner else None
        sfx = ensure_sfx(settings.sfx_dir) if wants_banner and request.enable_sfx else None

        started = time.monotonic()
        source, downloaded = self._obtain_source(request, job_id, notifier)
        fetch_seconds = time.monotonic() - started
        size_mb = source.stat().st_size / 1024 / 1024 if source.is_file() else 0.0
        self._log(
            job_id,
            f"{'Скачивание' if downloaded else 'Источник'}: {size_mb:.1f} МБ за {_duration(fetch_seconds)}"
            + ("" if downloaded else " (файл уже был на диске)"),
        )
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
            preview=request.preview,
        )
        clips = self._render_all(
            job_id, job_dir, source, info, schedule.starts, schedule.clip_duration, options,
            banner, sfx, render_start, notifier, on_clip,
        )  # fmt: skip

        render_seconds = time.monotonic() - started - fetch_seconds
        self._log(
            job_id,
            f"Итого по задаче: источник {_duration(fetch_seconds)} + анализ и рендер {_duration(render_seconds)}",
        )
        self._update(job_id, stage="Упаковка архива")
        self._progress(job_id, RENDER_END)
        notifier.touch()
        archive = job_dir / f"clips_{job_id}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:  # mp4 уже сжаты — просто складываем
            for clip in clips:
                bundle.write(job_dir / clip.filename, arcname=clip.filename)
        with self._lock:
            self._archives[job_id] = archive
        self._update(
            job_id,
            status=JobStatus.DONE,
            stage="Готово",
            progress=100.0,
            archive_name=archive.name,
            timing=f"источник {_duration(fetch_seconds)} · анализ и рендер {_duration(render_seconds)}",
        )
        self._log(job_id, f"Готово: роликов — {len(clips)}, архив {archive.name}")
        notifier.touch()

    def _schedule(
        self, request: JobRequest, job_id: str, source: Path, info: MediaInfo, start_offset: float, end_offset: float
    ) -> ClipSchedule:
        """Где в источнике лежат клипы. Режим «яркие моменты» ищет их по звуку, а если не вышло — раскладывает ровно."""
        highlights = request.slicing_mode is SlicingMode.HIGHLIGHTS
        if highlights:
            # Заставка, реклама и титры по звуку «ярче» разговора — без этого первый клип брали с самого начала серии.
            head, tail = edge_reserve(info.duration - start_offset - end_offset, request.num_clips * request.clip_duration)
            if head >= 1 or tail >= 1:
                self._log(
                    job_id,
                    f"Яркие моменты ищу не в первых {head:.0f} с и не в последних {tail:.0f} с: там обычно заставка, реклама и титры",
                )
            start_offset, end_offset = start_offset + head, end_offset + tail
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
        source = identify(url)
        if source is not None and cached_source_path(source.key, settings.cache_dir):
            self._update(job_id, stage="Видео из кеша")
            self._log(job_id, "Видео уже есть в кеше — повторно не скачиваю")
            notifier.touch()
            return download_video_cached(url, request.max_height, cache_dir=settings.cache_dir), False

        self._update(job_id, stage="Скачивание видео")
        where = source.platform.title if source is not None else "площадки"
        self._log(job_id, f"Скачиваю видео с {where} (до {request.max_height}p)…")
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

            started = time.monotonic()
            timeline = render_clip(
                source, job_dir / filename, start, clip_duration, options,
                info=info, banner=banner, sfx=sfx, on_progress=on_progress,
                threads=ffmpeg_threads(self.running_count()),
            )  # fmt: skip
            if self._normalize_audio:  # замерено: баннер громче контента на 2–8 дБ, а клип тише нормы платформ
                self._check_not_stopping(job_id)
                if normalize_loudness(job_dir / filename):
                    self._log(job_id, f"Клип {number}: звук выровнен до −14 LUFS")
            clip = ClipResult(
                index=number,
                filename=filename,
                source_start=round(start, 3),
                source_end=round(start + clip_duration, 3),
                duration=round(timeline.total_duration, 3),
                banner_points=[round(point, 3) for point in timeline.freeze_points],
                banner_windows=[(round(begin, 3), round(end, 3)) for begin, end in timeline.windows],
                render_seconds=round(time.monotonic() - started, 1),
            )
            clips.append(clip)
            with self._lock:  # клип виден сразу, не дожидаясь остальных
                self._jobs[job_id].clips.append(clip)
            speed = clip.duration / clip.render_seconds if clip.render_seconds else 0.0
            self._log(
                job_id,
                f"Клип {number} готов: {clip.duration:.1f} с за {_duration(clip.render_seconds)}"
                f" ({speed:.2f}× реального времени)",
            )
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
