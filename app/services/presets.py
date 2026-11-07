"""Пресеты нарезки: три готовых набора вместо двенадцати кнопок вручную.

Пресет — это те же `ClipSettings`, только собранные под конкретный жанр ролика. Правила простые
и взяты из практики, а не из головы:

* клип вместе с баннером укладывается в 59 секунд (Shorts блокирует более длинные при копирайт-претензии);
* «хук» — короткий и на весь экран: там важна картинка, а не поля;
* подкаст и стрим — с полями (размытый фон), потому что лицо не должно обрезаться;
* баннер у стрима внизу: сверху обычно интерфейс игры или титры.

Пресет никогда не делает непубликуемый клип: если баннеров слишком много, их число уменьшается.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import BannerPosition, ClipSettings, FrameMode
from app.services import publishing

MAX_HEIGHT = 1080  # скачиваем в Full HD: 4K только замедляет рендер, площадки всё равно ужмут
MIN_CLIP_SECONDS = 5.0  # короче клип уже не клип — тогда лучше убрать показ баннера


@dataclass(frozen=True)
class Preset:
    """Готовый набор настроек: что нарезать и как."""

    id: str  # короткий — едет в callback_data
    emoji: str
    title: str
    blurb: str  # одна строка для кнопки и экрана
    clips: int
    seconds: float  # чистая длина клипа, без пауз под баннер
    frame: FrameMode
    banners: int  # сколько показов баннера на клип
    position: BannerPosition


PRESETS: tuple[Preset, ...] = (
    Preset("hook", "⚡", "Хук для TikTok", "3 клипа по 25 с, кадр на весь экран", 3, 25.0, FrameMode.CENTER_CROP, 1, BannerPosition.CENTER),
    Preset("podcast", "🎙", "Подкаст", "4 клипа по 45 с, кадр с полями", 4, 45.0, FrameMode.BLUR_PAD, 1, BannerPosition.CENTER),
    Preset("stream", "🎮", "Стрим и игры", "2 клипа по 48 с, баннер снизу", 2, 48.0, FrameMode.BLUR_PAD, 1, BannerPosition.BOTTOM),
)  # fmt: skip
PRESET_BY_ID = {preset.id: preset for preset in PRESETS}


def settings_for(
    preset: Preset,
    base: ClipSettings,
    banner_seconds: float | None,
    *,
    source_seconds: float | None = None,
) -> ClipSettings:
    """Настройки пресета поверх прежних настроек человека.

    Что берём из пресета: число и длину клипов, кадр, число показов баннера и его место.
    Что оставляем человеческим: баннер (файл), звук баннера, качество скачивания, нарезку краёв.
    Длину подрезаем под лимит площадок и под длину самого ролика — пресет не должен ругаться на «не помещается».
    """
    banners = preset.banners if banner_seconds else 0
    pause = banner_seconds or 0.0
    seconds = preset.seconds
    # Сначала подрезаем клип — показ баннера важнее пары секунд контента; и только если не влезает
    # даже короткий клип, убираем показы.
    while banners > 0:
        room = publishing.LIMIT_SECONDS - banners * pause
        if room >= MIN_CLIP_SECONDS:
            seconds = min(seconds, room)
            break
        banners -= 1
    clips = preset.clips
    if source_seconds and source_seconds > 0:
        roomy = int(source_seconds // seconds) or 1  # клипы не должны пересекаться
        clips = max(1, min(clips, roomy))
    return ClipSettings(
        **{
            **base.model_dump(),
            "num_clips": clips,
            "clip_duration": round(seconds, 2),
            "frame_mode": preset.frame,
            "banner_count": banners,
            "banner_position": preset.position,
            "max_height": min(base.max_height, MAX_HEIGHT),
        }
    )
