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
    full_screen: bool  # кадр на весь экран (по лицу), если доступно


GOALS: dict[Goal, GoalSpec] = {
    Goal.TIKTOK: GoalSpec(
        Goal.TIKTOK, "⚡", "TikTok", "короткие и частые: ~25 с", 25.0, 2.0, 10, 1, True
    ),
    Goal.SHORTS: GoalSpec(
        Goal.SHORTS, "▶️", "YouTube Shorts", "подробнее: ~40 с", 40.0, 3.0, 8, 1, True
    ),
    Goal.UNIVERSAL: GoalSpec(
        Goal.UNIVERSAL, "🎯", "Универсально", "на любую площадку: ~30 с, видно весь кадр", 30.0, 4.0, 6, 2, False
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
    face_crop: bool,
) -> AutoPlan:
    """План нарезки. `base` — прежние настройки человека: берём из них всё, что автоподбор не трогает.

    `banner_seconds` — длина баннера (`None`, если баннера нет); `face_crop` — умеет ли эта установка
    искать лица, без чего «на весь экран» режет по центру кадра.
    """
    spec = GOALS[goal]
    clip = spec.clip_seconds
    shortened = False
    if 0 < duration < clip * 1.5:  # ролик почти как клип: берём его целиком
        clip = max(MIN_CLIP_SECONDS, float(math.floor(duration)))
        shortened = clip < spec.clip_seconds
    count = 1 if shortened else clip_count(duration, spec, clip)

    banners = spec.banners if banner_seconds else 0
    pause = banner_seconds or 0.0
    while banners > 1 and clip + banners * pause > SHORTS_LIMIT:
        banners -= 1

    frame = FrameMode.CENTER_CROP if spec.full_screen and face_crop else FrameMode.BLUR_PAD
    settings = ClipSettings.model_validate(
        {
            **base.model_dump(),
            "num_clips": count,
            "clip_duration": clip,
            "slicing_mode": SlicingMode.HIGHLIGHTS,
            "frame_mode": frame,
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
