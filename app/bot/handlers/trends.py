"""«🔥 В тренде»: что смотрят русскоязычные зрители на YouTube и RuTube. Выбрал ролик — сразу план нарезки под TikTok.

Данные кешируются на полчаса (`TrendCache`), так что первое нажатие за полчаса чуть ждёт, а следующие открываются
мгновенно. Выбор ролика ведёт в тот же `pick:`-обработчик, что и поиск по названию; отличается он только пометкой
`ORIGIN_KEY = ORIGIN_TREND` — по ней сразу строится автоплан.

Кнопки несут площадку в самих данных (`trends:<площадка>`, `trend:<площадка>:<жанр>`), поэтому выбор не надо
помнить в состоянии диалога — оно сбрасывается при каждом переходе.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot import keyboards, texts
from app.bot.context import (
    ORIGIN_KEY,
    ORIGIN_TREND,
    SEARCH_KEY,
    AppContext,
    edit_or_send,
    reset_dialog,
    safe_answer,
)
from app.bot.states import Flow
from app.services.platforms import PLATFORM_BY_ID, SEARCHABLE, Platform
from app.services.trends import CATEGORY_BY_ID, TrendsError, fetch_category

log = logging.getLogger(__name__)
router = Router(name="trends")

DEFAULT_PLATFORM = SEARCHABLE[0]


def _platform(platform_id: str) -> Platform | None:
    """Площадка с трендами по id из кнопки или `None`, если такой нет (или у неё нет трендов)."""
    platform = PLATFORM_BY_ID.get(platform_id)
    return platform if platform is not None and platform.search else None


@router.callback_query(F.data == "screen:trends")
@router.callback_query(F.data.startswith("trends:"))
async def cb_trends(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Главный экран раздела: переключатель площадки и жанры."""
    data = callback.data or ""
    platform = DEFAULT_PLATFORM if data == "screen:trends" else _platform(data.split(":", 1)[1])
    if platform is None:
        await safe_answer(callback, "Такой площадки нет — выберите из кнопок", alert=True)
        return
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)
    await edit_or_send(callback, texts.trends_home(platform), keyboards.trends_home(platform.id))


@router.callback_query(F.data.startswith("trend:"))
async def cb_category(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":")[1:]
    # Кнопки из старых сообщений («trend:<жанр>», когда площадка была одна) считаем кнопками YouTube.
    platform_id, category_id = parts if len(parts) == 2 else (DEFAULT_PLATFORM.id, parts[0])
    platform, category = _platform(platform_id), CATEGORY_BY_ID.get(category_id)
    if platform is None or category is None:
        await safe_answer(callback, "Такого раздела нет — выберите из кнопок", alert=True)
        return
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)

    key = f"{platform.id}:{category.id}"
    if ctx.trends.get(key) is None:
        # Холодный кеш: запрос к площадке идёт несколько секунд — говорим об этом, а не молчим.
        await edit_or_send(callback, texts.trends_loading(category, platform), None)
    try:
        videos = await ctx.trends.cached(key, lambda: fetch_category(category, platform.id))
    except TrendsError as error:
        await edit_or_send(callback, texts.error_text(str(error)), keyboards.trends_retry(platform.id, category.id))
        return
    except Exception:  # noqa: BLE001 — сеть и yt-dlp могут упасть нестандартно
        log.exception("Не удалось получить тренды: %s", key)
        await edit_or_send(
            callback,
            texts.error_text("Не получилось узнать, что в тренде. Попробуйте ещё раз."),
            keyboards.trends_retry(platform.id, category.id),
        )
        return

    if not videos:
        ctx.trends.forget(key)  # пустой список в кеше не держим: в следующий раз попробуем заново
    else:
        await state.update_data({SEARCH_KEY: videos, ORIGIN_KEY: ORIGIN_TREND})
        await state.set_state(Flow.choosing)
    await edit_or_send(callback, texts.trend_list(category, videos, platform), keyboards.trend_results(videos, platform.id))
