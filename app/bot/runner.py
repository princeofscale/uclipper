"""Мост между фоновыми задачами нарезки и asyncio-циклом бота.

`JobManager` выполняет нарезку в отдельном потоке и зовёт колбэки прямо из него. Здесь колбэки
аккуратно перекладываются в цикл событий, прогресс приходит не чаще раза в секунду, а готовые ролики
уходят пользователю сразу после рендера.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import Message

from app.bot import keyboards, texts
from app.bot.delivery import cleanup_job, deliver_results, send_clip
from app.bot.render import edit_html, send_html
from app.models import JobRequest, JobState, JobStatus
from app.services.jobs import JobManager
from app.services.user_store import UserStore

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.2  # как часто можно править сообщение-прогресс (Telegram не любит чаще ~1 раза в секунду)


@dataclass
class RunningJob:
    """Живая задача: её прогресс, сообщение в чате и уже отправленные клипы."""

    job_id: str
    chat_id: int
    user_id: int
    message: Message  # сообщение-прогресс, которое бот правит
    state: JobState
    sent_clips: set[int] = field(default_factory=set)  # индексы уже отправленных роликов
    last_progress_text: str = ""  # чтобы не править сообщение тем же текстом дважды
    finished: bool = False


class JobRunner:
    """Запускает задачи и общается с пользователем: прогресс, отдельные ролики, итог, отмена."""

    def __init__(self, bot: Bot, manager: JobManager, store: UserStore) -> None:
        self._bot = bot
        self._manager = manager
        self._store = store
        self._lock = asyncio.Lock()
        self._latest: dict[str, JobState] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._loop = asyncio.get_running_loop()
        self._jobs: dict[str, RunningJob] = {}

    # ---------- запуск ----------

    async def start(self, chat_id: int, user_id: int, request: JobRequest, progress_message: Message) -> str:
        """Ставит задачу в очередь и запускает слежение за её прогрессом."""
        loop = self._loop
        events: dict[str, asyncio.Event] = {}

        def on_update(state: JobState) -> None:  # зовётся из потока задачи
            self._latest[state.id] = state
            event = events.get(state.id)
            if event is not None:
                loop.call_soon_threadsafe(event.set)

        def on_clip(state: JobState) -> None:  # тоже из потока: только складываем состояние
            self._latest[state.id] = state
            event = events.get(state.id)
            if event is not None:
                loop.call_soon_threadsafe(event.set)

        # id задачи заранее неизвестен, поэтому событие создаём после submit по первому колбэку.
        job_id = self._manager.submit(request, on_update=on_update, on_clip=on_clip)
        event = asyncio.Event()
        events[job_id] = event
        state = self._manager.get(job_id) or JobState(id=job_id)
        running = RunningJob(job_id=job_id, chat_id=chat_id, user_id=user_id, message=progress_message, state=state)
        self._jobs[job_id] = running
        self._events[job_id] = event
        asyncio.create_task(self._watch(running, request))
        return job_id

    async def cancel(self, job_id: str) -> bool:
        running = self._jobs.get(job_id)
        if running is None or running.finished:
            return False
        return self._manager.cancel(job_id)

    def active_for_user(self, user_id: int) -> str | None:
        """id незаконченной задачи пользователя (у бота одна очередь, второй запуск не нужен)."""
        for job_id, running in self._jobs.items():
            if running.user_id == user_id and not running.finished:
                return job_id
        return None

    # ---------- слежение ----------

    async def _watch(self, running: RunningJob, request: JobRequest) -> None:
        """Правит сообщение-прогресс, отправляет клипы по мере готовности и подводит итог."""
        job_id = running.job_id
        event = self._events[job_id]
        try:
            while True:
                try:
                    await asyncio.wait_for(event.wait(), timeout=EDIT_INTERVAL)
                except (asyncio.TimeoutError, TimeoutError):
                    pass  # событие не пришло вовремя — работаем по таймеру
                event.clear()
                state = self._latest.get(job_id) or self._manager.get(job_id)
                if state is not None:
                    running.state = state
                    await self._send_new_clips(running)
                    if state.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
                        break
                    await self._edit_progress(running)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — слежение не должно падать молча
            log.exception("Ошибка слежения за задачей %s", job_id)

        await self._finish(running, request)

    async def _send_new_clips(self, running: RunningJob) -> None:
        """Отправляет ролики, которые уже отрендерились, — не дожидаясь конца задачи."""
        state = running.state
        if not state.clips:
            return
        for clip in state.clips:
            if clip.index in running.sent_clips:
                continue
            path = self._manager.clip_path(state.id, clip)
            if path is None:
                continue
            ok = await send_clip(
                self._bot,
                running.chat_id,
                clip,
                path,
                self._manager.job_dir(state.id),
                total=max(state.total_clips, len(state.clips)),
            )
            if ok:
                running.sent_clips.add(clip.index)

    async def _edit_progress(self, running: RunningJob) -> None:
        state = running.state
        text = texts.progress_text(state)
        if text == running.last_progress_text:
            return  # тот же процент и та же стадия — лишний запрос к Telegram не нужен
        running.last_progress_text = text
        markup = keyboards.running(state.id) if state.status is JobStatus.RUNNING else None
        try:
            await edit_html(running.message, text, markup)
        except Exception:  # noqa: BLE001 — сообщение могли удалить или текст не изменился
            log.debug("Не удалось обновить прогресс задачи %s", state.id, exc_info=True)

    async def _show_final(self, running: RunningJob, html: str, markup) -> None:
        """Финальный текст в сообщении-прогрессе; не получилось поправить — присылаем новым."""
        try:
            await edit_html(running.message, html, markup)
        except Exception:  # noqa: BLE001
            await send_html(self._bot, running.chat_id, html, markup)

    async def _finish(self, running: RunningJob, request: JobRequest) -> None:
        """Финальный экран, досылка оставшихся клипов и архив, уборка за собой."""
        job_id = running.job_id
        state = self._manager.get(job_id) or running.state
        running.state = state
        running.finished = True
        self._events.pop(job_id, None)
        self._latest.pop(job_id, None)
        try:
            if state.status is JobStatus.ERROR:
                await self._show_final(
                    running, texts.error_text(state.error or "Не удалось выполнить задачу."), keyboards.back_to_menu()
                )
                return

            if state.status is JobStatus.CANCELLED:
                await self._show_final(running, texts.CANCELLED, keyboards.back_to_menu())
                return

            # Досылаем только те клипы, которые не ушли во время рендера: уже отправленные не дублируем.
            report = await deliver_results(
                self._bot, running.chat_id, state, self._manager, already_sent=running.sent_clips
            )
            running.sent_clips.update(clip.index for clip in state.clips)
            if report.failed:
                await self._bot.send_message(
                    running.chat_id,
                    f"⚠️ {report.failed} ролик(ов) не удалось отправить: файл больше 50 МБ даже после сжатия. "
                    "Уменьшите длительность клипа или качество скачивания.",
                )
            summary = texts.results_summary(state)
            if not report.archive_sent and len(state.clips) > 1:
                summary += "<p><i>ZIP не отправлял: не влезает в лимит или ролики уже выше.</i></p>"
            await self._show_final(running, summary, keyboards.done())

            self._store.count_job(running.user_id, len(state.clips))
            self._store.remember_settings(running.user_id, request)
            self._store.save()
        finally:
            # Задачу убираем из живых при любом исходе: иначе отменённые и упавшие копятся в памяти.
            cleanup_job(self._manager, job_id)
            self._jobs.pop(job_id, None)
