"""Показ экранов: rich-сообщение с запасным вариантом на обычном HTML.

Экраны уходят rich-сообщениями (Bot API 10.1+), а если Telegram их не принял — тем же текстом обычным
HTML: пользователь не остаётся с мёртвой кнопкой из-за капризов разметки.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.rich import plain_text, rich_input, to_classic

log = logging.getLogger(__name__)

NOT_MODIFIED = "message is not modified"
_reported: set[str] = set()


def _rich_refused(error: Exception, html: str) -> None:
    """Один раз на каждый вид ошибки пишет в лог, что rich-вариант не прошёл (дальше — обычный HTML)."""
    key = str(error)[:160]
    if key not in _reported:
        _reported.add(key)
        log.warning("Telegram не принял rich-сообщение (%s) — отправляю обычным HTML: %r", key, plain_text(html)[:100])


async def send_html(bot: Bot, chat_id: int, html: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    """Новое сообщение в чат по id."""
    try:
        return await bot.send_rich_message(chat_id=chat_id, rich_message=rich_input(html), reply_markup=markup)
    except (TelegramBadRequest, TelegramNotFound) as error:
        _rich_refused(error, html)
    return await bot.send_message(chat_id, to_classic(html), reply_markup=markup)


async def show(message: Message, html: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    """Новое сообщение в ответ на сообщение пользователя."""
    try:
        return await message.answer_rich(rich_message=rich_input(html), reply_markup=markup)
    except (TelegramBadRequest, TelegramNotFound) as error:
        _rich_refused(error, html)
    return await message.answer(to_classic(html), reply_markup=markup)


async def edit_html(message: Message, html: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Правит сообщение бота. «Не изменилось» — не ошибка; прочие сбои пробрасываются наверх."""
    try:
        await message.edit_text(rich_message=rich_input(html), reply_markup=markup, parse_mode=None)
        return
    except TelegramBadRequest as error:
        if NOT_MODIFIED in str(error):
            return
        _rich_refused(error, html)
    await message.edit_text(to_classic(html), reply_markup=markup)


async def replace(message: Message, html: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Превращает сообщение бота («Смотрю ролик…») в готовый экран; не вышло — присылает экран новым."""
    try:
        await edit_html(message, html, markup)
    except Exception:  # noqa: BLE001 — сообщение удалено или слишком старое
        log.debug("Не удалось заменить сообщение — отправлю новое", exc_info=True)
        await send_html(message.bot, message.chat.id, html, markup)


async def edit_or_send(
    event: Message | CallbackQuery, html: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Правит сообщение, если это callback, иначе отправляет новое. Падение правки не роняет хэндлер."""
    if isinstance(event, CallbackQuery):
        target = event.message
        if target is not None:
            try:
                if target.text is None and target.rich_message is None and target.caption is not None:
                    await target.edit_caption(caption=to_classic(html)[:1024], reply_markup=markup)
                else:
                    await edit_html(target, html, markup)
                return
            except TelegramBadRequest as error:
                if NOT_MODIFIED in str(error):
                    return  # экран уже такой, как надо — второй копии не нужно
                log.debug("Не удалось поправить сообщение — отправлю новое", exc_info=True)
            except Exception:  # noqa: BLE001 — сообщение удалено/слишком старое и прочие мелочи Telegram
                log.debug("Не удалось поправить сообщение — отправлю новое", exc_info=True)
        chat_id = target.chat.id if target is not None else event.from_user.id
        await send_html(event.bot, chat_id, html, markup)
        return
    await show(event, html, markup)


async def safe_answer(event: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Отвечает на callback: без этого Telegram крутит часики на кнопке."""
    try:
        await event.answer(text, show_alert=alert)
    except Exception:  # noqa: BLE001 — callback мог устареть, это не ошибка
        log.debug("Callback уже неактуален", exc_info=True)
