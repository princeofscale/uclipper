"""Общие вещи для хэндлеров: контекст приложения, черновик в FSM, показ экранов.

Показ экранов (rich-сообщения с запасным HTML) живёт в `app/bot/render.py`; здесь его имена
реэкспортируются, чтобы хэндлерам хватало одного импорта.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot import texts
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
from app.models import ClipSettings, Draft
from app.services.banner_manager import BannerInfo, default_banner
from app.services.jobs import JobManager
from app.services.trends import TrendCache
from app.services.user_store import UserStore

log = logging.getLogger(__name__)

DRAFT_KEY = "draft"
INPUT_KEY = "input_field"  # какое числовое поле сейчас ждёт текстовый ввод
SEARCH_KEY = "search"  # найденные ролики (поиск или тренды), из которых пользователь выбирает
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
    banner_preview_id: str | None = None  # file_id уже отправленного превью баннера: повторно файл не грузим

    @property
    def workspace_dir(self) -> Path:
        return self.settings.workspace_dir

    def banner(self) -> BannerInfo | None:
        """Главный баннер проекта — единственный, который показывает бот (`None`, если файла нет)."""
        return default_banner(self.settings.banners_dir)

    def banner_seconds(self) -> float | None:
        """Сколько играет баннер (`None`, если баннера нет)."""
        banner = self.banner()
        return banner.duration if banner is not None and banner.duration else None

    def banners(self) -> list[BannerInfo]:
        banner = self.banner()
        return [banner] if banner is not None else []

    def banner_name(self, banner_id: str | None) -> str | None:
        """Человеческое имя баннера по id (или сам id, если файла уже нет)."""
        if not banner_id:
            return None
        banner = self.banner()
        return banner.name if banner is not None and banner.id == banner_id else banner_id


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


async def need_draft(callback: CallbackQuery, state: FSMContext) -> Draft | None:
    """Черновик или `None` — тогда пользователь уже получил понятное объяснение."""
    draft = await get_draft(state)
    if draft is None:
        await safe_answer(callback, texts.LOST_DRAFT, alert=True)
    return draft


def update_settings(settings: ClipSettings, **changes) -> ClipSettings:
    """Меняет настройки с полной валидацией (model_copy(update=…) её пропускает)."""
    return ClipSettings(**{**settings.model_dump(), **changes})
