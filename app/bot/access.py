"""Закрытый доступ: если задан `ALLOWED_USERS`, бот отвечает только перечисленным людям.

Нарезка видео грузит процессор сервера, а токен бота не секретен для тех, кто знает его username,
поэтому на сервере бота стоит ограничить. Пустой список — бот открыт для всех (как раньше).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import texts

log = logging.getLogger(__name__)

ALLOWED_USERS_ENV = "ALLOWED_USERS"


def parse_allowed_users(raw: str | None) -> frozenset[int]:
    """`"123, 456;789"` → {123, 456, 789}. Всё, что не число, пропускается с предупреждением."""
    users: set[int] = set()
    for part in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            users.add(int(part))
        except ValueError:
            log.warning("%s: «%s» — не Telegram ID, пропускаю.", ALLOWED_USERS_ENV, part)
    return frozenset(users)


def allowed_users_from_env() -> frozenset[int]:
    return parse_allowed_users(os.getenv(ALLOWED_USERS_ENV))


class AccessMiddleware(BaseMiddleware):
    """Пропускает только своих. Чужому говорит его ID — владельцу останется вписать его в `.env`."""

    def __init__(self, allowed: frozenset[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not self.allowed or (user is not None and user.id in self.allowed):
            return await handler(event, data)
        user_id = user.id if user is not None else 0
        log.info("Отказ в доступе пользователю %s", user_id)
        notice = texts.NOT_ALLOWED.format(user_id=user_id)
        if isinstance(event, Message):
            await event.answer(notice)
        elif isinstance(event, CallbackQuery):
            await event.answer("Закрытый бот", show_alert=True)
        return None
