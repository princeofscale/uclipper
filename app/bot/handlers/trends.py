"""«🔥 В тренде»: что смотрят русскоязычные зрители на YouTube и RuTube — на одной площадке или сразу на обеих.
Выбрал ролик — сразу план нарезки под TikTok.

Данные кешируются на полчаса (`TrendCache`) отдельно по площадке и жанру, так что «Везде» и выбор одной площадки
делят один кеш: первое нажатие за полчаса чуть ждёт, а следующие открываются мгновенно. Выбор ролика ведёт
в тот же `pick:`-обработчик, что и поиск по названию; отличается он только пометкой `ORIGIN_KEY = ORIGIN_TREND` —
по ней сразу строится автоплан.

Кнопки несут выбор в самих данных (`trends:<площадка|all>`, `trend:<площадка|all>:<жанр>`), поэтому выбор не надо
помнить в состоянии диалога — оно сбрасывается при каждом переходе.

Если одна из площадок не ответила, остальные всё равно показываются — с пометкой, кого не хватает; ошибка
на весь экран только когда не ответил никто.
"""
from __future__ import annotations

import asyncio
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
    refuse_busy,
    reset_dialog,
    safe_answer,
)
from app.bot.states import Flow
from app.services.platforms import EVERYWHERE, TRENDING, YOUTUBE, Platform, scope
from app.services.search import RESULTS, interleave
from app.services.trends import CATEGORY_BY_ID, Category, TrendsError, fetch_category

log = logging.getLogger(__name__)
router = Router(name="trends")

NOBODY_ANSWERED = "Ни одна площадка сейчас не отвечает — попробуйте через пару минут."
FAILED = "Не получилось узнать, что в тренде. Попробуйте ещё раз."


def _key(platform: Platform, category: Category) -> str:
    return f"{platform.id}:{category.id}"


async def _platform_trends(ctx: AppContext, platform: Platform, category: Category) -> list[dict]:
    return await ctx.trends.cached(_key(platform, category), lambda: fetch_category(category, platform.id))


async def _collect(
    ctx: AppContext, platforms: tuple[Platform, ...], category: Category
) -> tuple[list[dict], list[Platform], str]:
    """Тренды жанра со всех `platforms` разом: (ролики вперемешку, кто не ответил, текст для экрана ошибки).

    Текст нужен, только когда не ответил никто: для одной площадки это её собственная причина, для нескольких —
    общая. Пустые ответы в кеше не держим — в следующий раз спросим заново.
    """
    answers = await asyncio.gather(*(_platform_trends(ctx, p, category) for p in platforms), return_exceptions=True)
    lists: list[list[dict]] = []
    missing: list[Platform] = []
    reasons: list[str] = []
    for platform, answer in zip(platforms, answers, strict=True):
        if isinstance(answer, BaseException):
            if not isinstance(answer, Exception):  # отмена и прочее служебное — не наше дело глотать
                raise answer
            missing.append(platform)
            if isinstance(answer, TrendsError):
                reasons.append(str(answer))
            else:
                log.error("Не удалось получить тренды: %s", _key(platform, category), exc_info=answer)
                reasons.append(FAILED)
        elif answer:
            lists.append(answer)
        else:
            ctx.trends.forget(_key(platform, category))
    reason = reasons[0] if len(platforms) == 1 and reasons else NOBODY_ANSWERED
    return interleave(lists, RESULTS), missing, reason


@router.callback_query(F.data == "screen:trends")
@router.callback_query(F.data.startswith("trends:"))
async def cb_trends(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    """Главный экран раздела: выбор площадки и жанры."""
    data = callback.data or ""
    choice = EVERYWHERE if data == "screen:trends" else data.split(":", 1)[1]
    if scope(choice, TRENDING) is None:
        await safe_answer(callback, "Такой площадки нет — выберите из кнопок", alert=True)
        return
    busy = ctx.busy_for(callback.from_user.id)
    if busy is not None:  # выбрать ролик из трендов всё равно не дадут: нарезка сейчас недоступна
        await refuse_busy(callback, state, ctx, busy)
        return
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)
    await edit_or_send(callback, texts.trends_home(choice), keyboards.trends_home(choice))


@router.callback_query(F.data.startswith("trend:"))
async def cb_category(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":")[1:]
    # Кнопки из старых сообщений («trend:<жанр>», когда площадка была одна) считаем кнопками YouTube.
    if len(parts) == 1:
        parts = [YOUTUBE.id, parts[0]]
    platforms = scope(parts[0], TRENDING) if len(parts) == 2 else None
    category = CATEGORY_BY_ID.get(parts[-1])
    if platforms is None or category is None:
        await safe_answer(callback, "Такого раздела нет — выберите из кнопок", alert=True)
        return
    choice = parts[0]
    await safe_answer(callback)
    await reset_dialog(state, ctx, callback.from_user.id)

    if any(ctx.trends.get(_key(platform, category)) is None for platform in platforms):
        # Холодный кеш: запрос к площадке идёт несколько секунд — говорим об этом, а не молчим.
        await edit_or_send(callback, texts.trends_loading(category, choice), None)
    videos, missing, reason = await _collect(ctx, platforms, category)

    if len(missing) == len(platforms):  # не ответил никто
        await edit_or_send(callback, texts.error_text(reason), keyboards.trends_retry(choice, category.id))
        return
    if videos:
        await state.update_data({SEARCH_KEY: videos, ORIGIN_KEY: ORIGIN_TREND})
        await state.set_state(Flow.choosing)
    await edit_or_send(
        callback,
        texts.trend_list(category, videos, choice, missing=missing),
        keyboards.trend_results(videos, choice, category.id),
    )
