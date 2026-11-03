"""Общие вещи для хэндлеров: контекст приложения, черновик в FSM, показ экранов."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import keyboards, texts
from app.bot.runner import JobRunner
from app.config import Settings
from app.models import ClipSettings, Draft
from app.services.banner_manager import BannerInfo, list_banners
from app.services.jobs import JobManager
from app.services.user_store import UserStore

log = logging.getLogger(__name__)

DRAFT_KEY = "draft"


@dataclass
class AppContext:
    """Всё, что нужно хэндлерам: настройки, очередь задач, баннеры, мост к Telegram."""

    settings: Settings
    manager: JobManager
    store: UserStore
    runner: JobRunner

    @property
    def workspace_dir(self) -> Path:
        return self.settings.workspace_dir

    def banners(self) -> list[BannerInfo]:
        """Все баннеры: главный, пресеты и свои — в стабильном порядке."""
        return list_banners(self.settings.banners_dir)

    def banner_name(self, banner_id: str | None) -> str | None:
        """Человеческое имя баннера по id (или сам id, если файла уже нет)."""
        if not banner_id:
            return None
        return next((banner.name for banner in self.banners() if banner.id == banner_id), banner_id)


# ---------- черновик ----------


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


async def drop_draft(state: FSMContext) -> None:
    await state.update_data({DRAFT_KEY: None})


def update_settings(settings: ClipSettings, **changes) -> ClipSettings:
    """Меняет настройки с полной валидацией (model_copy(update=…) её пропускает)."""
    return ClipSettings(**{**settings.model_dump(), **changes})


async def remember_settings(ctx: AppContext, user_id: int, draft: Draft) -> None:
    """Запоминает настройки пользователя, чтобы они применялись к следующему ролику."""
    ctx.store.remember_settings(user_id, draft.settings)


# ---------- удобные обёртки ----------


async def show(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    return await message.answer(text, reply_markup=markup)


async def edit_or_send(
    event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None, *, keep_photo: bool = False
) -> None:
    """Правит сообщение, если это callback, иначе отправляет новое. Падение правки не роняет хэндлер."""
    if isinstance(event, CallbackQuery) and event.message is not None:
        target = event.message
        try:
            if keep_photo:
                await target.edit_caption(caption=text, reply_markup=markup)
            else:
                if target.text is not None:
                    await target.edit_text(text, reply_markup=markup)
                else:
                    await target.edit_caption(caption=text, reply_markup=markup)
            return
        except Exception:  # noqa: BLE001 — «message is not modified» и прочие мелочи Telegram
            log.debug("Не удалось поправить сообщение — отправлю новое", exc_info=True)
    await event.answer(text, reply_markup=markup)


async def safe_answer(event: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Отвечает на callback: без этого Telegram крутит часики на кнопке."""
    try:
        await event.answer(text, show_alert=alert)
    except Exception:  # noqa: BLE001 — callback мог устареть, это не ошибка
        log.debug("Callback уже неактуален", exc_info=True)


def clip_settings_from_request(request: Any) -> Any:
    """Совместимость: настройки из JobRequest (наследует ClipSettings) без источника."""
    from app.models import ClipSettings

    return ClipSettings(**{key: getattr(request, key) for key in ClipSettings.model_fields})


TERMS_URL = "https://core.telegram.org/bots/api"
