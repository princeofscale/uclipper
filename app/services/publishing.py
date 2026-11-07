"""Публикация: влезает ли готовый клип в лимиты площадок.

Правило простое и проверенное по первоисточникам:

* YouTube блокирует Shorts длиннее минуты, если на них придёт любая копирайт-претензия
  (справка YouTube: «Shorts longer than one minute that have an active copyright claim… will be blocked»),
  а наши клипы собраны из чужого видео, так что претензия — обычное дело;
* TikTok не показывает в ленте «Для вас» перерезанное чужое видео без своих правок, поэтому клип
  не должен быть ещё и слишком длинным: 45–59 с — рабочий диапазон.

Отсюда одно число: клип вместе с паузами под баннер не длиннее 59 секунд.
"""
from __future__ import annotations

from app.models import BannerMode, ClipSettings

LIMIT_SECONDS = 59.0  # столько может идти готовый клип вместе с баннерами
LONG_CLIP_SECONDS = 60.0  # с этой длины клип уже считается «длинным» для Shorts


def total_seconds(settings: ClipSettings, banner_seconds: float | None) -> float:
    """Длина готового клипа: видео плюс паузы под баннер (в режиме «поверх» клип не удлиняется)."""
    if settings.banner_count and banner_seconds and settings.banner_mode is BannerMode.FREEZE:
        return settings.clip_duration + settings.banner_count * banner_seconds
    return settings.clip_duration


def fits(settings: ClipSettings, banner_seconds: float | None) -> bool:
    """Помещается ли клип в лимит площадок."""
    return total_seconds(settings, banner_seconds) <= LIMIT_SECONDS


def verdict(settings: ClipSettings, banner_seconds: float | None) -> str:
    """Одна строка для интерфейса: помещается или нет. Без разметки — её вставляет вызывающий."""
    total = total_seconds(settings, banner_seconds)
    if total <= LIMIT_SECONDS:
        return f"{total:.0f} с — влезает в Shorts и TikTok"
    return (
        f"{total:.0f} с — не влезает в Shorts: при копирайт-претензии ролик блокируют. "
        f"Обрежьте до {LIMIT_SECONDS:.0f} с или уберите часть показов баннера"
    )
