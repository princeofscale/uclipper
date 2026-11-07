"""Приём источника: ссылка на ролик любой площадки, поиск по названию, присланный файл и выбор из найденного.

Любой принятый ролик начинает диалог заново: прежний черновик, ожидание ввода и списки результатов
забываются, а человек видит выбор режима (или сразу автоплан, если ролик выбран в «В тренде»).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import keyboards, texts
from app.bot.context import (
    ORIGIN_KEY,
    ORIGIN_SEARCH,
    ORIGIN_TREND,
    QUERY_KEY,
    SCOPE_KEY,
    SEARCH_KEY,
    AppContext,
    edit_or_send,
    menu_view,
    put_draft,
    replace,
    safe_answer,
    show,
    update_settings,
)
from app.bot.handlers.common import ask_query
from app.bot.handlers.modes import auto_view, mode_view
from app.bot.states import Flow
from app.config import TELEGRAM_MAX_UPLOAD_MB, VIDEO_EXTS
from app.models import ClipSettings, Draft, VideoInfo
from app.services.downloader import DownloadError, fetch_video_info, save_local_upload
from app.services.platforms import EVERYWHERE, SEARCHABLE, identify, mentions_known_site, scope
from app.services.search import QueryTooShort, search_videos
from app.services.video_processor import MediaError, probe_media

log = logging.getLogger(__name__)
router = Router(name="source")

# «Похоже на ссылку», но площадку мы не знаем (или это не ролик) — тогда честно скажем, что не подошло.
_URL_HINT = re.compile(r"^(https?://|www\.)|\.[a-z]{2,}/", re.IGNORECASE)


def _base_settings(ctx: AppContext, user_id: int) -> ClipSettings:
    """Прежние настройки человека вместе с его баннером.

    Баннер — тот, что человек выбрал на экране «🎬 Баннеры», если такой файл ещё на месте; иначе главный.
    Если файлов нет вовсе, клипы идут без вставки — иначе задача не запустилась бы.
    """
    settings = ctx.store.settings(user_id)
    banner = ctx.banner(settings.banner_id) or ctx.banner()
    if banner is None:
        return update_settings(settings, banner_id=None, banner_count=0)
    return update_settings(settings, banner_id=banner.id)


async def open_draft(
    state: FSMContext, ctx: AppContext, draft: Draft, *, auto: bool = False, user_id: int | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Начинает диалог с этим роликом и возвращает первый экран: выбор режима или (`auto`) готовый план."""
    html, markup = auto_view(draft, ctx) if auto else mode_view(draft, ctx, user_id=user_id)
    await state.set_data({})  # всё прежнее в этом диалоге — черновик, ввод, списки — больше не нужно
    await put_draft(state, draft)
    await state.set_state(Flow.editing)
    return html, markup


async def open_video(
    state: FSMContext, ctx: AppContext, user_id: int, info: dict, *, auto: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Ролик по ссылке (сведения `info`) → первый экран диалога."""
    video = VideoInfo(
        url=info["url"],
        title=info.get("title") or "Без названия",
        duration=float(info.get("duration") or 0),
        platform=info.get("platform") or None,
        uploader=info.get("uploader") or None,
        thumbnail=info.get("thumbnail") or None,
    )
    return await open_draft(
        state, ctx, Draft.from_video(video, _base_settings(ctx, user_id)), auto=auto, user_id=user_id
    )


# ---------- ссылка или название ----------


async def _accepts_source(message: Message, state: FSMContext, ctx: AppContext) -> bool:
    """Текст, который надо принять как источник: ссылка или название.

    В меню-состояниях (и в пустом, после перезапуска бота) подходит любой текст. С экрана настроек
    и из списка результатов — только ссылка: она важнее ожидаемого числа (числом ссылка быть не может),
    поэтому новый ролик заменяет прежний, даже если бот как раз просил ввести число.
    """
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    current = await state.get_state()
    if current == Flow.running.state:
        # Состояние «идёт нарезка» само не сбрасывается: считаем занятым только пока задача реально идёт.
        return not ctx.runner.active_for_user(message.from_user.id)
    if current in (None, Flow.waiting_link.state, Flow.waiting_query.state, Flow.waiting_file.state):
        return True
    if current in (Flow.editing.state, Flow.choosing.state):
        return identify(text) is not None
    return False


async def _not_busy(message: Message, ctx: AppContext) -> bool:
    """Файл принимаем, если у пользователя не идёт нарезка."""
    return not ctx.runner.active_for_user(message.from_user.id)


async def _refuse_while_busy(message: Message, ctx: AppContext) -> bool:
    """Бот ведёт чужую нарезку (или на диске мало места): ролик не принимаем, а показываем красное меню с причиной.

    Диалог не сбрасывается: если человек перед этим настраивал свой ролик, черновик ждёт его на прежнем экране.
    Числа для настроек сюда не попадают — `_accepts_source` пропускает с экрана настроек только ссылки.
    """
    if ctx.busy_for(message.from_user.id) is None:
        return False
    html, markup = menu_view(ctx, message.from_user.id)
    await show(message, html, markup)
    return True


@router.message(_accepts_source, F.text)
async def on_text(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if await _refuse_while_busy(message, ctx):
        return
    text = (message.text or "").strip()
    source = identify(text)
    if source is not None:
        await _from_url(message, state, ctx, source.url)
        return
    current = await state.get_state()
    looks_like_link = mentions_known_site(text) or _URL_HINT.search(text)
    if looks_like_link and current != Flow.waiting_query.state:
        await show(message, texts.BAD_LINK, keyboards.back_to_menu())
        return
    # Не ссылка — считаем, что это название для поиска.
    await _search(message, state, ctx, text)


async def _from_url(message: Message, state: FSMContext, ctx: AppContext, url: str) -> None:
    status = await show(message, texts.FETCHING)
    try:
        info = await asyncio.to_thread(fetch_video_info, url)
    except DownloadError as error:
        await replace(status, texts.error_text(str(error)), keyboards.back_to_menu())
        return
    except Exception:  # noqa: BLE001 — сеть/yt-dlp могут упасть нестандартно
        log.exception("Не удалось получить сведения о ролике")
        await replace(
            status,
            texts.error_text("Не получилось получить сведения о ролике. Попробуйте ещё раз."),
            keyboards.back_to_menu(),
        )
        return
    html, markup = await open_video(state, ctx, message.from_user.id, info)
    await replace(status, html, markup)


# ---------- поиск по названию ----------


async def _chosen_scope(state: FSMContext) -> str:
    """Площадка, выбранная на экране «Что искать?»; не выбрана (или выбор устарел) — искать везде."""
    choice = (await state.get_data()).get(SCOPE_KEY) or EVERYWHERE
    return choice if scope(choice, SEARCHABLE) is not None else EVERYWHERE


async def _run_search(state: FSMContext, query: str, choice: str) -> tuple[str, InlineKeyboardMarkup]:
    """Ищет `query` там, где сказано в `choice`, запоминает итог в диалоге и возвращает экран.

    Находка — список с выбором площадки под ним; неудача — причина и тот же выбор площадки (запрос запомнен,
    так что человек одной кнопкой повторит его в другом месте). Слишком короткий запрос запоминать нечего.
    """
    problem: str | None = None
    results: list[dict] = []
    try:
        results = await asyncio.to_thread(search_videos, query, platform=choice)
        if not results:
            raise DownloadError(f"По запросу «{query}» ничего не нашлось — попробуйте другое название или пришлите ссылку.")
    except QueryTooShort as error:
        await state.set_state(Flow.waiting_query)
        return texts.error_text(str(error)), keyboards.query_prompt(choice)
    except DownloadError as error:
        problem = texts.error_text(str(error))
    except Exception:  # noqa: BLE001 — сеть и yt-dlp могут упасть нестандартно
        log.exception("Поиск по названию упал")
        problem = texts.error_text("Поиск не удался. Попробуйте ещё раз или пришлите ссылку.")
    if problem is not None:
        await state.set_data({QUERY_KEY: query, SCOPE_KEY: choice})
        await state.set_state(Flow.waiting_query)
        return problem, keyboards.search_results([], choice)
    await state.set_data({SEARCH_KEY: results, ORIGIN_KEY: ORIGIN_SEARCH, QUERY_KEY: query, SCOPE_KEY: choice})
    await state.set_state(Flow.choosing)
    return texts.search_cards(results, choice), keyboards.search_results(results, choice)


async def _search(message: Message, state: FSMContext, ctx: AppContext, query: str) -> None:
    choice = await _chosen_scope(state)
    status = await show(message, texts.searching(query, choice))
    html, markup = await _run_search(state, query, choice)
    await replace(status, html, markup)


@router.callback_query(F.data.startswith("qs:"))
async def cb_search_again(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Тот же запрос на другой площадке — кнопкой под результатами."""
    choice = (callback.data or "").split(":", 1)[1]
    if scope(choice, SEARCHABLE) is None:
        await safe_answer(callback, "Такой площадки нет — выберите из кнопок", alert=True)
        return
    query = (await state.get_data()).get(QUERY_KEY)
    if not query:  # диалог сбросился (например, бот перезапускался): спрашиваем запрос заново
        await ask_query(callback, state, ctx, choice)
        return
    await safe_answer(callback)
    await edit_or_send(callback, texts.searching(query, choice), None)
    html, markup = await _run_search(state, query, choice)
    await edit_or_send(callback, html, markup)


# ---------- выбор найденного ----------


@router.callback_query(Flow.choosing, F.data.startswith("pick:"))
async def on_pick(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Ролик из поиска или из «В тренде». Из трендов сразу показываем автоплан под TikTok."""
    data = await state.get_data()
    results = data.get(SEARCH_KEY) or []
    try:
        index = int((callback.data or "pick:-1").split(":", 1)[1])
        entry = results[index]
    except (ValueError, IndexError):
        await safe_answer(callback, "Вариант устарел — поищите ещё раз", alert=True)
        return
    await safe_answer(callback)
    info = {
        "platform": entry.get("platform"),
        "url": entry["url"],
        "title": entry.get("title"),
        "duration": entry.get("duration"),
        "uploader": entry.get("uploader"),
        "thumbnail": entry.get("thumbnail"),
    }
    html, markup = await open_video(
        state, ctx, callback.from_user.id, info, auto=data.get(ORIGIN_KEY) == ORIGIN_TREND
    )
    await edit_or_send(callback, html, markup)


# ---------- видеофайл ----------


@router.message(_not_busy, F.video | F.document | F.animation)
async def on_file(message: Message, state: FSMContext, ctx: AppContext) -> None:
    """Приём видеофайла: до 20 МБ (лимит облачного Bot API)."""
    if await _refuse_while_busy(message, ctx):
        return
    media = message.video or message.document or message.animation
    if media is None:
        return
    name = getattr(message.video, "file_name", None) or getattr(media, "file_name", None) or "video.mp4"
    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_EXTS:
        await show(message, texts.NOT_A_VIDEO, keyboards.back_to_menu())
        return
    if media.file_size and media.file_size > TELEGRAM_MAX_UPLOAD_MB * 1024 * 1024:
        await show(message, texts.FILE_TOO_BIG, keyboards.back_to_menu())
        return

    status = await show(message, texts.DOWNLOADING_FILE)
    uploads_dir = ctx.settings.uploads_dir
    try:
        temp = uploads_dir / f"incoming_{message.from_user.id}_{media.file_unique_id}{suffix}"
        await message.bot.download(media, destination=temp, timeout=120)
        upload_id, path = await asyncio.to_thread(_store_upload, temp, name, uploads_dir)
        info = await asyncio.to_thread(probe_media, path)
    except DownloadError as error:
        await replace(status, texts.error_text(str(error)), keyboards.back_to_menu())
        return
    except MediaError:
        await replace(status, texts.FILE_BAD.format(error="файл не читается как видео"), keyboards.back_to_menu())
        return
    except Exception:  # noqa: BLE001
        log.exception("Не удалось принять видеофайл")
        await replace(
            status,
            texts.error_text("Не получилось принять файл. Попробуйте ещё раз или пришлите ссылку на ролик."),
            keyboards.back_to_menu(),
        )
        return

    draft = Draft.from_upload(
        upload_id, Path(name).stem or "Видео", info.duration, _base_settings(ctx, message.from_user.id)
    )
    html, markup = await open_draft(state, ctx, draft, user_id=message.from_user.id)
    await replace(status, html, markup)


def _store_upload(temp: Path, original_name: str, uploads_dir: Path) -> tuple[str, Path]:
    """Кладёт временный файл как обычную загрузку (`save_local_upload`) и убирает временный."""
    try:
        with temp.open("rb") as stream:
            upload_id, path = save_local_upload(original_name, stream, uploads_dir)
        return upload_id, path
    finally:
        temp.unlink(missing_ok=True)
