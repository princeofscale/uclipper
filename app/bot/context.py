"""Общие вещи для хэндлеров: контекст приложения, черновик в FSM, показ экранов.

Показ экранов (rich-сообщения с запасным HTML) живёт в `app/bot/render.py`; здесь его имена
реэкспортируются, чтобы хэндлерам хватало одного импорта.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot import keyboards, texts
from app.bot.busy import Busy, Cause
from app.bot.render import (  # noqa: F401 — реэкспорт
    edit_html,
    edit_or_send,
    replace,
    safe_answer,
    send_html,
    show,
)
from app.bot.runner import JobRunner
from app.bot.states import Flow
from app.config import Settings
from app.models import ClipSettings, Draft, JobStatus
from app.services.banner_manager import BannerInfo, catalog_banners, default_banner, find_banner
from app.services.capacity import disk_is_low, jobs_limit_reached
from app.services.jobs import JobManager
from app.services.trends import TrendCache
from app.services.user_store import UserStore

log = logging.getLogger(__name__)

DRAFT_KEY = "draft"
INPUT_KEY = "input_field"  # какое числовое поле сейчас ждёт текстовый ввод
SEARCH_KEY = "search"  # найденные ролики (поиск или тренды), из которых пользователь выбирает
QUERY_KEY = "search_query"  # что искали по названию: чтобы повторить поиск на другой площадке
SCOPE_KEY = "search_scope"  # где искать по названию: id площадки или «all» (везде)
ORIGIN_KEY = "search_origin"  # откуда список: поиск или тренды — от этого зависит, что покажем после выбора
ORIGIN_SEARCH = "search"  # ролики найдены по названию → дальше выбор режима
ORIGIN_TREND = "trend"  # ролики из «В тренде» → сразу автоплан под TikTok


@dataclass
class AppContext:
    """Всё, что нужно хэндлерам: настройки, очередь задач, баннер, тренды, мост к Telegram."""

    settings: Settings
    manager: JobManager
    store: UserStore
    runner: JobRunner
    trends: TrendCache = field(default_factory=TrendCache)
    banner_previews: dict[str, str] = field(default_factory=dict)  # file_id отправленных превью по id баннера

    def banners(self) -> list[BannerInfo]:
        """Баннеры проекта, которые сейчас лежат в папке (TGrader и TgRader v2)."""
        return catalog_banners(self.settings.banners_dir)

    def banner(self, banner_id: str | None = None) -> BannerInfo | None:
        """Баннер по id, а без id — главный (`None`, если такого файла нет)."""
        if banner_id:
            return find_banner(banner_id, self.settings.banners_dir)
        return default_banner(self.settings.banners_dir)

    def banner_seconds(self, banner_id: str | None = None) -> float | None:
        """Сколько играет баннер (`None`, если баннера нет)."""
        banner = self.banner(banner_id)
        return banner.duration if banner is not None and banner.duration else None

    def busy_for(self, user_id: int) -> Busy | None:
        """Почему этому человеку сейчас нельзя начать нарезку, или `None`, если можно.

        Бот ведёт один ролик за раз (`MAX_JOBS`), а у каждого человека не больше одной нарезки; кроме того, нарезки
        не принимаются, пока на диске мало места. Занятость длится до тех пор, пока клипы не доставлены в чат.
        """
        runner = self.runner
        job_id = runner.active_for_user(user_id)
        if job_id is not None:
            state = runner.state_of(job_id)
            stoppable = state is None or state.status in (JobStatus.QUEUED, JobStatus.RUNNING)  # готовую уже не остановить
            return Busy(
                Cause.MINE,
                progress=state.progress if state else 0.0,
                stage=state.stage if state else "",
                job_id=job_id if stoppable else None,
            )
        if runner.has_claim(user_id):  # нажал «Нарезать» дважды: первый запуск ещё идёт
            return Busy(Cause.MINE, stage="Запускаю")
        if jobs_limit_reached(runner.in_flight()):
            state = runner.oldest_state()
            return Busy(Cause.OTHERS, progress=state.progress if state else 0.0, stage=state.stage if state else "")
        if disk_is_low(self.settings.workspace_dir):
            return Busy(Cause.DISK)
        return None


# ---------- черновик и диалог ----------


async def get_draft(state: FSMContext) -> Draft | None:
    data = await state.get_data()
    raw = data.get(DRAFT_KEY)
    if not raw:
        return None
    try:
        return Draft.model_validate(raw)
    except ValueError:  # старый формат после обновления бота
        log.warning("Не удалось прочитать черновик из FSM — начну заново")
        return None


async def put_draft(state: FSMContext, draft: Draft) -> None:
    await state.update_data({DRAFT_KEY: draft.model_dump(mode="json")})


async def clear_input(state: FSMContext) -> None:
    """Снимает ожидание числа: иначе обычный текст на другом экране приняли бы за ввод."""
    await state.update_data({INPUT_KEY: None})


async def reset_dialog(state: FSMContext, ctx: AppContext, user_id: int) -> None:
    """Чистый лист: черновик, ожидание ввода и списки результатов забываются.

    Так делают «🏠 В начало», `/start` и любая другая команда. Идущая нарезка не прерывается:
    пока она работает, диалог остаётся в состоянии «занят».
    """
    busy = ctx.runner is not None and ctx.runner.active_for_user(user_id)
    await state.clear()
    await state.set_state(Flow.running if busy else Flow.waiting_link)


def menu_view(ctx: AppContext, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Главное меню таким, каким его сейчас надо показать: свободное или красное с пояснением, почему бот занят."""
    busy = ctx.busy_for(user_id)
    return texts.start(busy), keyboards.main_menu(busy)


async def refuse_busy(callback: CallbackQuery, state: FSMContext, ctx: AppContext, busy: Busy) -> None:
    """Нажата кнопка, которая сейчас не работает: всплывающее окно объясняет причину, на экране — актуальное меню.

    Кнопка могла остаться на старом экране, пока бот был свободен, поэтому меню перерисовывается заново.
    """
    await safe_answer(callback, texts.busy_alert(busy), alert=True)
    await reset_dialog(state, ctx, callback.from_user.id)
    html, markup = menu_view(ctx, callback.from_user.id)
    await edit_or_send(callback, html, markup)


async def need_draft(callback: CallbackQuery, state: FSMContext) -> Draft | None:
    """Черновик или `None` — тогда пользователь уже получил понятное объяснение."""
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, texts.LOST_DRAFT, alert=True)
    return draft


def update_settings(settings: ClipSettings, **changes) -> ClipSettings:
    """Меняет настройки с полной валидацией (model_copy(update=…) её пропускает)."""
    return ClipSettings(**{**settings.model_dump(), **changes})
