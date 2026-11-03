"""«🔥 В тренде»: что смотрят русскоязычные зрители. Выбрал ролик — сразу план нарезки под TikTok.

Данные берутся с YouTube и кешируются на полчаса (`TrendCache`), так что первое нажатие за полчаса
чуть ждёт, а следующие открываются мгновенно. Выбор ролика ведёт в тот же `pick:`-обработчик, что и
поиск по названию; отличается он только пометкой `ORIGIN_KEY = ORIGIN_TREND` — по ней сразу строится автоплан.
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
from app.services.trends import CATEGORY_BY_ID, TrendsError, fetch_category

log = logging.getLogger(__name__)
router = Router(name="trends")


@router.callback_query(F.data == "screen:trends")
async def cb_trends(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)
    await edit_or_send(callback, texts.trends_home(), keyboards.trends_home())


@router.callback_query(F.data.startswith("trend:"))
async def cb_category(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    category = CATEGORY_BY_ID.get((callback.data or "trend:").split(":", 1)[1])
    if category is None:
        await safe_answer(callback, "Такого жанра нет — выберите из кнопок", alert=True)
        return
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)

    key = f"category:{category.id}"
    if ctx.trends.get(key) is None:
        # Холодный кеш: запрос к YouTube идёт несколько секунд — говорим об этом, а не молчим.
        await edit_or_send(callback, texts.trends_loading(category), None)
    try:
        videos = await ctx.trends.cached(key, lambda: fetch_category(category))
    except TrendsError as error:
        await edit_or_send(callback, texts.error_text(str(error)), keyboards.trends_retry(category.id))
        return
    except Exception:  # noqa: BLE001 — сеть и yt-dlp могут упасть нестандартно
        log.exception("Не удалось получить тренды: %s", category.id)
        await edit_or_send(
            callback,
            texts.error_text("Не получилось узнать, что в тренде. Попробуйте ещё раз."),
            keyboards.trends_retry(category.id),
        )
        return

    if not videos:
        ctx.trends.forget(key)  # пустой список в кеше не держим: в следующий раз попробуем заново
    else:
        await state.update_data({SEARCH_KEY: videos, ORIGIN_KEY: ORIGIN_TREND})
        await state.set_state(Flow.choosing)
    await edit_or_send(callback, texts.trend_list(category, videos), keyboards.trend_results(videos))
