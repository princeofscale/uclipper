"""Мост между фоновыми задачами нарезки и asyncio-циклом бота.

`JobManager` выполняет нарезку в отдельном потоке и зовёт колбэки прямо из него. Здесь колбэки
аккуратно перекладываются в цикл событий, прогресс приходит не чаще раза в секунду, а готовые ролики
уходят пользователю сразу после рендера.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import Message

from app.bot import keyboards, texts
from app.bot.delivery import cleanup_job, deliver_results, send_clip
from app.bot.render import edit_html, send_html
from app.models import JobRequest, JobState, JobStatus
from app.services.history import History, Record
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
    started_at: float = field(default_factory=time.monotonic)  # от этого момента считается «прошло»
    moved_at: float = field(default_factory=time.monotonic)  # когда у человека на глазах в последний раз что-то менялось
    last_key: tuple | None = None  # что именно было видно в тот момент: стадия, процент, число готовых клипов


class JobRunner:
    """Запускает задачи и общается с пользователем: прогресс, отдельные ролики, итог, отмена."""

    def __init__(self, bot: Bot, manager: JobManager, store: UserStore, history: History | None = None) -> None:
        self._bot = bot
        self._manager = manager
        self._store = store
        self._history = history
        self._lock = asyncio.Lock()
        self._events: dict[str, asyncio.Event] = {}
        self._loop = asyncio.get_running_loop()
        self._jobs: dict[str, RunningJob] = {}  # по порядку запуска; задача уходит отсюда, когда клипы доставлены
        self._claims: set[int] = set()  # люди, нажавшие «Нарезать»: место занято, а сама задача ещё создаётся

    # ---------- запуск ----------

    async def start(self, chat_id: int, user_id: int, request: JobRequest, progress_message: Message) -> str:
        """Ставит задачу в очередь и запускает слежение за её прогрессом."""
        loop = self._loop
        events: dict[str, asyncio.Event] = {}

        def wake(state: JobState) -> None:  # зовётся из потока задачи: только будит слежение
            # Само состояние берётся живым у менеджера. Присланный снимок хранить нельзя: между колбэками задача
            # может минутами менять стадию молча (анализ ролика), и снимок показывал бы давно прошедшее.
            event = events.get(state.id)
            if event is not None:
                loop.call_soon_threadsafe(event.set)

        on_update = on_clip = wake

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

    async def cancel(self, job_id: str, *, after_current: bool = False) -> bool:
        """Остановить задачу. `after_current` — доделать текущий клип и отдать готовые."""
        running = self._jobs.get(job_id)
        if running is None or running.finished:
            return False
        return self._manager.cancel(job_id, after_current=after_current)

    def active_for_user(self, user_id: int) -> str | None:
        """id нарезки человека, которая ещё не закончена: идёт сейчас или бот досылает ему готовые клипы."""
        for job_id, running in self._jobs.items():
            if running.user_id == user_id:
                return job_id
        return None

    # ---------- занятость: бот ведёт ограниченное число нарезок сразу (по умолчанию одну) ----------

    def claim(self, user_id: int) -> None:
        """Занимает место за человеком, нажавшим «Нарезать».

        Зовётся синхронно сразу после проверки «свободно ли»: пока обработчик ждёт ответа Telegram, задачи ещё нет,
        и без заявки двое, нажавшие одновременно, прошли бы проверку оба.
        """
        self._claims.add(user_id)

    def release(self, user_id: int) -> None:
        """Снимает заявку: задача уже создана (она занимает место сама) или запуск не удался."""
        self._claims.discard(user_id)

    def has_claim(self, user_id: int) -> bool:
        return user_id in self._claims

    def in_flight(self) -> int:
        """Сколько нарезок занимают бот: идущие, досылающие клипы и только что принятые."""
        return len(self._jobs) + len(self._claims)

    def state_of(self, job_id: str) -> JobState | None:
        """Свежее состояние живой нарезки (`None`, если такой уже нет)."""
        running = self._jobs.get(job_id)
        if running is None:
            return None
        return self._manager.get(job_id) or running.state

    def oldest_state(self) -> JobState | None:
        """Состояние самой давней из живых нарезок: её прогресс видят те, кому бот сейчас отказывает."""
        for job_id in self._jobs:
            return self.state_of(job_id)
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
                state = self._manager.get(job_id)
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
        waiting = state.status is JobStatus.QUEUED
        now = time.monotonic()
        key = (state.status, state.stage, round(state.progress), len(state.clips))
        if key != running.last_key:  # у человека на глазах что-то изменилось: молчание отсчитывается заново
            running.last_key, running.moved_at = key, now
        text = texts.progress_text(
            state,
            ahead=self._manager.ahead_of(state.id) if waiting else 0,
            elapsed=now - running.started_at,
            quiet=now - running.moved_at,
        )
        if text == running.last_progress_text:
            return  # тот же процент и та же стадия — лишний запрос к Telegram не нужен
        running.last_progress_text = text
        # Уйти можно и из очереди: ждать чужие задачи никто не обязан.
        markup = (
            keyboards.running(state.id, soft=state.status is JobStatus.RUNNING)
            if waiting or state.status is JobStatus.RUNNING
            else None
        )
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

    def _remember_history(self, running: RunningJob, request: JobRequest, state: JobState) -> None:
        """Дописывает готовые клипы в историю нарезок (`workspace/history.jsonl`).

        Файл нужен не «для отчётности»: из него берутся экран «🕘 История», оценки 👍/👎 и будущая кнопка
        «нарезать иначе». Размер файла берём до уборки папки задачи — после неё файлов уже нет.
        """
        if self._history is None:
            return
        now = time.strftime("%Y-%m-%dT%H:%M")
        total = max(state.total_clips, len(state.clips))
        for clip in state.clips:
            path = self._manager.clip_path(state.id, clip)
            size_mb = round(path.stat().st_size / 1024 / 1024, 1) if path is not None else 0.0
            self._history.append(
                Record(
                    ts=now,
                    user_id=running.user_id,
                    job_id=state.id,
                    index=clip.index,
                    of=total,
                    platform=request.platform,
                    source=request.source_key,
                    title=request.title,
                    mode=request.mode or "manual",
                    start=clip.source_start,
                    end=clip.source_end,
                    clip_len=clip.duration,
                    banner=request.banner_id,
                    banner_count=request.banner_count,
                    render_s=clip.render_seconds,
                    size_mb=size_mb,
                )
            )

    async def _finish(self, running: RunningJob, request: JobRequest) -> None:
        """Финальный экран, досылка оставшихся клипов и архив, уборка за собой."""
        job_id = running.job_id
        state = self._manager.get(job_id) or running.state
        running.state = state
        running.finished = True
        self._events.pop(job_id, None)
        try:
            if state.status is JobStatus.ERROR:
                await self._show_final(
                    running, texts.error_text(state.error or "Не удалось выполнить задачу."), keyboards.back_to_menu()
                )
                return

            if state.status is JobStatus.CANCELLED and state.soft_stop and state.clips:
                # Остановились «после клипа»: готовое не выбрасываем — досылаем и подводим итог.
                await deliver_results(
                    self._bot, running.chat_id, state, self._manager, already_sent=running.sent_clips
                )
                running.sent_clips.update(clip.index for clip in state.clips)
                await self._show_final(
                    running,
                    texts.soft_stopped(len(state.clips)),
                    keyboards.done(texts.clip_description(request, state)),
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
            await self._show_final(running, summary, keyboards.done(texts.clip_description(request, state)))

            self._store.count_job(running.user_id, len(state.clips))
            self._store.remember_settings(running.user_id, request)
            self._store.save()
            await asyncio.to_thread(self._remember_history, running, request, state)
        finally:
            # Задачу убираем из живых при любом исходе: иначе отменённые и упавшие копятся в памяти.
            cleanup_job(self._manager, job_id)
            self._jobs.pop(job_id, None)
