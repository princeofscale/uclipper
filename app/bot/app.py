"""Сборка бота: конфиг из окружения, зависимости, роутеры, запуск polling."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommandScopeAllPrivateChats, ErrorEvent, MenuButtonDefault

from app.bot import texts
from app.bot.access import AccessMiddleware, allowed_users_from_env
from app.bot.context import AppContext
from app.bot.handlers import banners, common, fallback, jobs, settings_screen, source
from app.bot.runner import JobRunner
from app.config import Settings
from app.services.banner_manager import ensure_default_banner, ensure_presets, ensure_sfx, ensure_strip_presets
from app.services.janitor import max_age_days_from_env, sweep_forever
from app.services.jobs import JobManager
from app.services.user_store import UserStore

log = logging.getLogger(__name__)

MANAGED_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
PROXY_ENV = "TELEGRAM_PROXY"

# Что видно в пустом чате до первого нажатия «Старт» (команд у бота нет — только кнопки).
BOT_DESCRIPTION = (
    "Нарезаю YouTube-ролики на вертикальные клипы 9:16 и вклеиваю рекламный баннер.\n"
    "Нажмите «Старт» и пришлите ссылку — всё остальное кнопками."
)
BOT_SHORT_DESCRIPTION = "Нарезка YouTube на вертикальные клипы с баннером"


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


async def _on_error(event: ErrorEvent) -> bool:
    """Любая необработанная ошибка: в лог с трассировкой, пользователю — короткое «не вышло» вместо тишины."""
    log.error("Необработанная ошибка в хэндлере", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer("Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
        elif update.message is not None:
            await update.message.answer(texts.error_text("Что-то пошло не так. Попробуйте ещё раз."))
    except Exception:  # noqa: BLE001 — уведомить не вышло, ошибка уже в логе
        log.debug("Не удалось сообщить пользователю об ошибке", exc_info=True)
    return True


def build_dispatcher(ctx: AppContext | None = None, allowed_users: frozenset[int] = frozenset()) -> Dispatcher:
    """Собирает диспетчер: хранилище состояний, доступ, роутеры в правильном порядке."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    if ctx is not None:
        dispatcher["ctx"] = ctx
    if allowed_users:
        access = AccessMiddleware(allowed_users)
        dispatcher.message.outer_middleware(access)
        dispatcher.callback_query.outer_middleware(access)
    dispatcher.errors.register(_on_error)
    # Порядок важен: баннеры идут раньше приёма видео, иначе «пришлите свой баннер» перехватит
    # общий обработчик видеофайлов и сохранит картинку как источник клипа. Запасной роутер — последний.
    for router in (
        common.router,
        banners.router,
        source.router,
        settings_screen.router,
        jobs.router,
        fallback.router,
    ):
        _attach(dispatcher, router)
    return dispatcher


def build_app(
    settings: Settings | None = None, allowed_users: frozenset[int] = frozenset()
) -> tuple[AppContext, JobManager, Dispatcher]:
    """Готовит рабочие папки, баннеры по умолчанию, очередь и диспетчер (без сети).

    Запускатор задач подставляется в `run` — здесь он ещё не может существовать: нужен `Bot`.
    """
    settings = settings or Settings.default()
    settings.ensure_dirs()
    ensure_presets(settings.banners_dir)
    ensure_strip_presets(settings.banners_dir)  # без полос «постоянный баннер» нечем включить
    ensure_default_banner(settings.banners_dir)
    ensure_sfx(settings.sfx_dir)

    manager = JobManager(settings)
    store = UserStore(settings.workspace_dir)
    ctx = AppContext(settings=settings, manager=manager, store=store, runner=None)  # type: ignore[arg-type]
    return ctx, manager, build_dispatcher(ctx, allowed_users)


async def prepare_bot(bot: Bot) -> None:
    """Убирает меню команд (интерфейс — только кнопки) и задаёт описание пустого чата."""
    try:
        await bot.delete_my_commands()
        await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except Exception:  # noqa: BLE001 — косметика: бот работает и без неё
        log.warning("Не удалось обновить профиль бота", exc_info=True)


async def run() -> None:
    """Точка входа: собирает всё и запускает long polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    token = token_from_env()
    allowed = allowed_users_from_env()
    ctx, manager, dispatcher = build_app(allowed_users=allowed)
    proxy = (os.getenv(PROXY_ENV) or "").strip()
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ctx.runner = JobRunner(bot, manager, ctx.store)
    dispatcher["ctx"] = ctx
    await prepare_bot(bot)
    me = await bot.me()
    log.info(
        "Бот @%s (%s) запущен%s%s",
        me.username,
        me.full_name,
        f", доступ: {len(allowed)} чел." if allowed else ", доступ открыт для всех",
        ", через прокси" if proxy else "",
    )
    sweeper = asyncio.create_task(sweep_forever(ctx.settings, max_age_days_from_env()))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        sweeper.cancel()
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
