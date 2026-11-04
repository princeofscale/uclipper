"""Автоподбор нарезки: по длине ролика и цели («TikTok», «Shorts») выбирает число и длину клипов.

Это правила, а не нейросеть: человек видит план и может поправить его вручную. Правила простые:
короткий клип держит внимание лучше, длинный ролик даёт больше клипов, а итоговый клип вместе с
баннером должен укладываться в рамки платформы.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.models import (
    BannerMode,
    BannerPosition,
    ClipSettings,
    FrameMode,
    Goal,
    SlicingMode,
)

MIN_CLIP_SECONDS = 5.0  # короче клип уже не клип
SHORTS_LIMIT = 59.0  # итоговая длина вместе с баннером для Shorts и Reels, с
LONG_SOURCE_MINUTES = 90  # от этой длины качаем в 720p: скачивание и рендер иначе слишком долгие
EDGE_RESERVE = 0.92  # около 8% ролика уходит на заставку и титры — на них клипы не рассчитываем
DEFAULT_COUNT = 5  # если длина ролика неизвестна


@dataclass(frozen=True)
class GoalSpec:
    goal: Goal
    emoji: str
    label: str
    blurb: str  # одна строка в интерфейсе
    clip_seconds: float  # длина клипа без баннера
    minutes_per_clip: float  # один клип на столько минут исходного ролика
    max_clips: int
    banners: int  # показов баннера на клип


GOALS: dict[Goal, GoalSpec] = {
    # TikTok и Shorts: клип от 45 с без баннера — короче зрителя не удержать; с баннером всё равно влезает в 59 с.
    Goal.TIKTOK: GoalSpec(
        Goal.TIKTOK, "⚡", "TikTok", "динамичные клипы по ~45 с", 45.0, 2.0, 8, 1
    ),
    Goal.SHORTS: GoalSpec(
        Goal.SHORTS, "▶️", "YouTube Shorts", "подробнее: клипы по ~50 с", 50.0, 3.0, 6, 1
    ),
    Goal.UNIVERSAL: GoalSpec(
        Goal.UNIVERSAL, "🎯", "Универсально", "на любую площадку: ~30 с, два баннера", 30.0, 4.0, 6, 2
    ),
}


@dataclass(frozen=True)
class AutoPlan:
    settings: ClipSettings
    spec: GoalSpec
    shortened: bool  # ролик короткий: клип пришлось сделать короче обычного


def clip_count(duration: float, spec: GoalSpec, clip_seconds: float) -> int:
    """Сколько клипов делать: по одному на `minutes_per_clip` минут, но так, чтобы они не пересекались."""
    if duration <= 0:
        return DEFAULT_COUNT
    wanted = round(duration / 60 / spec.minutes_per_clip)
    roomy = int(duration * EDGE_RESERVE // clip_seconds)
    return max(1, min(wanted, spec.max_clips, roomy))


def plan_auto(
    duration: float,
    goal: Goal,
    base: ClipSettings,
    *,
    banner_seconds: float | None,
) -> AutoPlan:
    """План нарезки. `base` — прежние настройки человека: берём из них всё, что автоподбор не трогает.

    `banner_seconds` — длина баннера (`None`, если баннера нет). Кадр всегда один: горизонтальное видео целиком по
    центру, сверху и снизу размытое продолжение картинки. «На весь экран» растягивало бы полосу в треть кадра
    (из 720p — 405×720 до 1080×1920), и клип выходил мыльным; это по-прежнему можно выбрать вручную.
    """
    spec = GOALS[goal]
    clip = spec.clip_seconds
    shortened = False
    whole = 0 < duration < clip * 1.5  # ролик почти как клип: берём его целиком
    if whole:
        clip = max(MIN_CLIP_SECONDS, float(math.floor(duration)))
        shortened = clip < spec.clip_seconds
    count = 1 if whole else clip_count(duration, spec, clip)

    banners = spec.banners if banner_seconds else 0
    pause = banner_seconds or 0.0
    while banners > 1 and clip + banners * pause > SHORTS_LIMIT:
        banners -= 1
    room = SHORTS_LIMIT - banners * pause  # сколько секунд остаётся самому клипу
    if clip > room >= MIN_CLIP_SECONDS:  # целый ролик в 52–67 с вместе с баннером вылез бы за предел
        clip = float(math.floor(room))

    settings = ClipSettings.model_validate(
        {
            **base.model_dump(),
            "num_clips": count,
            "clip_duration": clip,
            "slicing_mode": SlicingMode.HIGHLIGHTS,
            "frame_mode": FrameMode.BLUR_PAD,
            "auto_trim": True,
            "start_offset": 0.0,
            "end_offset": 0.0,
            "banner_count": banners,
            "banner_mode": BannerMode.FREEZE,
            "banner_position": BannerPosition.CENTER,
            "banner_scale": 85,
            "banner_sound": True,
            "enable_sfx": False,
            "max_height": 720 if duration > LONG_SOURCE_MINUTES * 60 else 1080,
        }
    )
    return AutoPlan(settings=settings, spec=spec, shortened=shortened)
