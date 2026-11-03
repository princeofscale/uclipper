"""Фейковый Telegram: подменяет HTTP-сессию aiogram, чтобы проверять бота без сети.

Все вызовы Bot API записываются в `FakeSession.requests`, а в ответ приходят правдоподобные объекты
(Message, File, User). Этого хватает, чтобы прогонять настоящий Dispatcher и настоящие хэндлеры.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    Message,
    PhotoSize,
    Update,
    User,
)

from app.bot.app import build_app
from app.bot.context import AppContext
from app.bot.runner import JobRunner
from app.config import Settings

FAKE_TOKEN = "42:TEST-TOKEN"
ME = {"id": 42, "is_bot": True, "first_name": "ClipBanner", "username": "clipbanner_test_bot", "can_join_groups": True}

_counter = {"message": 100, "update": 1}


def _next_message_id() -> int:
    _counter["message"] += 1
    return _counter["message"]


def _chat(chat_id: int) -> Chat:
    return Chat(id=chat_id, type="private", first_name="Тест")


def _message(bot: Bot, chat_id: int | None = None, text: str | None = None, **extra) -> Message:
    """Собирает Message-ответ на метод Bot API так, как его вернул бы Telegram (с привязкой к боту)."""
    payload: dict[str, Any] = {
        "message_id": _next_message_id(),
        "date": int(dt.datetime.now().timestamp()),
        "chat": _chat(chat_id or 1).model_dump(),
    }
    if text is not None:
        payload["text"] = text
    payload.update(extra)
    # context={"bot": bot} обязателен: aiogram монтирует ответ к боту, иначе message.answer() упадёт.
    return Message.model_validate(payload, context={"bot": bot})


class FakeSession(BaseSession):
    """Сессия, которая не ходит в сеть: отвечает на методы из `handlers` или общими заглушками."""

    def __init__(self) -> None:
        super().__init__(
            api=TelegramAPIServer.from_base("http://fake"),
            json_loads=json.loads,
            json_dumps=json.dumps,
        )
        self.requests: list[TelegramMethod] = []
        self.handlers: dict[str, Any] = {}
        self.stream_bytes: bytes = b""
        self.closed = False

    # ---------- Bot API ----------

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.requests.append(method)
        name = method.__api_method__
        handler = self.handlers.get(name)
        if handler is not None:
            return handler(method, bot)
        return self._default(method, bot)

    def _default(self, method, bot):
        name = method.__api_method__
        chat_id = getattr(method, "chat_id", None)
        if name == "getMe":
            return User.model_validate(ME)
        if name in ("sendMessage", "editMessageText", "sendPhoto", "sendDocument", "sendVideo"):
            return _message(bot, chat_id, getattr(method, "text", None) or getattr(method, "caption", None))
        if name == "getFile":
            return File(file_id=method.file_id, file_unique_id="uniq", file_size=1024, file_path="videos/file.mp4")
        if name in ("deleteMessage", "setMyCommands", "answerCallbackQuery", "editMessageReplyMarkup"):
            return True
        if name == "editMessageMedia":
            return _message(bot, chat_id)
        if name == "getUpdates":
            return []
        raise AssertionError(f"Фейковая сессия не знает метод {name}")

    async def stream_content(self, url: str, headers=None, timeout: int = 30, chunk_size: int = 65536,
                             raise_for_status: bool = True):  # fmt: skip
        yield self.stream_bytes

    async def close(self) -> None:
        self.closed = True

    # ---------- удобства для проверок ----------

    def calls(self, api_method: str) -> list[TelegramMethod]:
        return [request for request in self.requests if request.__api_method__ == api_method]

    def texts(self) -> list[str]:
        """Все тексты, которые бот отправил или вписал в сообщения (без HTML-разметки проверяем сами)."""
        found = []
        for request in self.requests:
            text = getattr(request, "text", None) or getattr(request, "caption", None)
            if text:
                found.append(text)
        return found

    def last_text(self) -> str:
        texts = self.texts()
        return texts[-1] if texts else ""

    def alerts(self) -> list[str]:
        """Тексты всплывающих подсказок (alert=True в answerCallbackQuery)."""
        found = []
        for request in self.calls("answerCallbackQuery"):
            text = getattr(request, "text", None)
            if text:
                found.append(text)
        return found

    def last_alert(self) -> str:
        alerts = self.alerts()
        return alerts[-1] if alerts else ""

    def buttons(self) -> list[str]:
        """callback_data всех кнопок из последней отправленной/отредактированной клавиатуры."""
        data: list[str] = []
        for request in reversed(self.requests):
            markup = getattr(request, "reply_markup", None)
            if markup is None:
                continue
            for row in getattr(markup, "inline_keyboard", []):
                for button in row:
                    if getattr(button, "callback_data", None):
                        data.append(button.callback_data)
            if data:
                return data
        return data


class Harness:
    """Готовая связка: настройки во временной папке, бот с фейковой сессией и диспетчер."""

    def __init__(self, settings: Settings) -> None:
        self.session = FakeSession()
        self.bot = Bot(
            token=FAKE_TOKEN,
            session=self.session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.ctx, self.manager, self.dispatcher = build_app(settings)
        self.ctx.runner = JobRunner(self.bot, self.manager, self.ctx.store)
        self.dispatcher["ctx"] = self.ctx

    async def close(self) -> None:
        await self.session.close()

    # ---------- апдейты ----------

    async def send(self, text: str, *, user_id: int = 1, chat_id: int | None = None) -> None:
        """Шлёт боту текстовое сообщение от пользователя."""
        chat_id = chat_id or user_id
        update = self._update(
            message={
                "message_id": _next_message_id(),
                "date": int(dt.datetime.now().timestamp()),
                "chat": _chat(chat_id).model_dump(),
                "from": {"id": user_id, "is_bot": False, "first_name": "Тест", "language_code": "ru"},
                "text": text,
            }
        )
        await self.feed(update)

    async def press(self, data: str, *, user_id: int = 1, chat_id: int | None = None, message_id: int | None = None) -> None:
        """Нажимает inline-кнопку."""
        chat_id = chat_id or user_id
        update = self._update(
            callback_query={
                "id": f"cb{_counter['update']}",
                "from": {"id": user_id, "is_bot": False, "first_name": "Тест", "language_code": "ru"},
                "chat_instance": "test",
                "data": data,
                "message": {
                    "message_id": message_id or _next_message_id(),
                    "date": int(dt.datetime.now().timestamp()),
                    "chat": _chat(chat_id).model_dump(),
                    "text": "сообщение бота",
                },
            }
        )
        await self.feed(update)

    def _user_fields(self, user_id: int) -> dict:
        return {
            "chat": _chat(user_id).model_dump(),
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест", "language_code": "ru"},
        }

    async def send_file(self, *, user_id: int = 1, file_id: str = "file-1", file_name: str = "video.mp4",
                        file_size: int = 1024) -> None:  # fmt: skip
        """Шлёт боту документ (видеофайл)."""
        await self._send_media(
            user_id,
            "document",
            {"file_id": file_id, "file_unique_id": f"uniq-{file_id}", "file_name": file_name,
             "file_size": file_size, "mime_type": "video/mp4"},
        )

    async def send_video_file(self, *, user_id: int = 1, file_id: str = "video-1", file_name: str = "video.mp4",
                              file_size: int = 1024) -> None:  # fmt: skip
        """Шлёт боту видео как `message.video` (так Telegram отдаёт обычное видео)."""
        await self._send_media(
            user_id,
            "video",
            {"file_id": file_id, "file_unique_id": f"uniq-{file_id}", "file_name": file_name,
             "file_size": file_size, "mime_type": "video/mp4", "width": 640, "height": 360, "duration": 8},
        )

    async def send_photo(self, *, user_id: int = 1, file_id: str = "photo-1", file_size: int = 1000) -> None:
        """Шлёт боту фото (баннер-картинку)."""
        await self._send_media(
            user_id,
            "photo",
            [{"file_id": file_id, "file_unique_id": f"uniq-{file_id}", "width": 640, "height": 240,
              "file_size": file_size}],
        )

    async def _send_media(self, user_id: int, field: str, payload) -> None:
        await self.feed(
            self._update(
                message={
                    "message_id": _next_message_id(),
                    "date": int(dt.datetime.now().timestamp()),
                    **self._user_fields(user_id),
                    field: payload,
                }
            )
        )

    def _update(self, **fields) -> Update:
        payload = {"update_id": _counter["update"], **fields}
        _counter["update"] += 1
        return Update.model_validate(payload, context={"bot": self.bot})

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)


def fake_user(user_id: int = 1) -> User:
    return User(id=user_id, is_bot=False, first_name="Тест", language_code="ru")


def fake_photo(file_id: str = "photo-1") -> PhotoSize:
    return PhotoSize(file_id=file_id, file_unique_id=f"uniq-{file_id}", width=100, height=100, file_size=1000)


__all__ = ["FakeSession", "Harness", "FAKE_TOKEN", "fake_user", "fake_photo", "CallbackQuery"]
