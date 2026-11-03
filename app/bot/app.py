"""Сборка бота: конфиг из окружения, зависимости, роутеры, запуск polling."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot import keyboards, texts
from app.bot.context import AppContext
from app.bot.handlers import banners, common, jobs, settings_screen, source
from app.bot.runner import JobRunner
from app.config import Settings
from app.services.banner_manager import ensure_default_banner, ensure_presets, ensure_sfx
from app.services.jobs import JobManager
from app.services.user_store import UserStore

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="new", description="Новый ролик"),
    BotCommand(command="settings", description="Настройки нарезки"),
    BotCommand(command="banners", description="Баннеры"),
    BotCommand(command="mystats", description="Моя статистика"),
    BotCommand(command="start", description="Начать заново"),
    BotCommand(command="help", description="Справка"),
]

MANAGED_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def token_from_env() -> str:
    """Токен бота: из окружения или `.env`. Без токена бот не запускается — падаем с понятным текстом."""
    token = (os.getenv(MANAGED_TOKEN_ENV) or "").strip()
    if not token:
        _load_dotenv()
        token = (os.getenv(MANAGED_TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(
            "Не задан токен бота.\n"
            "Положите его в переменную окружения TELEGRAM_BOT_TOKEN или в файл .env в корне проекта:\n"
            "    TELEGRAM_BOT_TOKEN=123456:ABC-…"
        )
    if ":" not in token:
        raise SystemExit("Токен выглядит неправильно: он должен быть вида 123456789:AA… — проверьте .env")
    return token


def _load_dotenv() -> None:
    """Читает `.env` из корня проекта (если установлен python-dotenv и файл существует)."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        log.warning("Файл .env есть, но python-dotenv не установлен — читаю только переменные окружения.")
        return
    load_dotenv(path, override=False)


def _attach(dispatcher: Dispatcher, router) -> None:
    """Подключает роутер к диспетчеру, отцепляя его от прежнего (нужно тестам: бота собирают много раз).

    Роутеры — модульные синглтоны с декораторами, а aiogram запрещает подключать один роутер к двум
    диспетчерам. В бою диспетчер собирается один раз, но тесты поднимают отдельного бота на каждый сценарий.
    """
    previous = router.parent_router
    if previous is not None:
        previous.sub_routers.remove(router)
        router._parent_router = None  # noqa: SLF001 — другого способа отцепить роутер у aiogram нет
    dispatcher.include_router(router)


def build_dispatcher(ctx: AppContext | None = None) -> Dispatcher:
    """Собирает диспетчер: хранилище состояний и роутеры в правильном порядке."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    if ctx is not None:
        dispatcher["ctx"] = ctx
    # Порядок важен: баннеры идут раньше приёма видео, иначе «пришлите свой баннер» перехватит
    # общий обработчик видеофайлов и сохранит картинку как источник клипа.
    for router in (common.router, banners.router, source.router, settings_screen.router, jobs.router):
        _attach(dispatcher, router)
    return dispatcher


def build_app(settings: Settings | None = None) -> tuple[AppContext, JobManager, Dispatcher]:
    """Готовит рабочие папки, баннеры по умолчанию, очередь и диспетчер (без сети).

    Запускатор задач подставляется в `run` — здесь он ещё не может существовать: нужен `Bot`.
    """
    settings = settings or Settings.default()
    settings.ensure_dirs()
    ensure_presets(settings.banners_dir)
    ensure_default_banner(settings.banners_dir)
    ensure_sfx(settings.sfx_dir)

    manager = JobManager(settings)
    store = UserStore(settings.workspace_dir)
    ctx = AppContext(settings=settings, manager=manager, store=store, runner=None)  # type: ignore[arg-type]
    return ctx, manager, build_dispatcher(ctx)


async def run(tok_path_env: str = MANAGED_TOKEN_ENV) -> None:
    """Точка входа: собирает всё, регистрирует команды и запускает long polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    token = token_from_env()
    ctx, manager, dispatcher = build_app()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ctx.runner = JobRunner(bot, manager, ctx.store)
    dispatcher["ctx"] = ctx
    await bot.set_my_commands(BOT_COMMANDS)
    me = await bot.me()
    log.info("Бот @%s (%s) запущен", me.username, me.full_name)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        manager.shutdown()
        ctx.store.save()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nБот остановлен.")


if __name__ == "__main__":
    main()
