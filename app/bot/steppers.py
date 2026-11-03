"""Числовые настройки, которые меняются кнопками: пресеты, «−/+» и (по желанию) ввод своего числа.

Здесь только данные и проверка границ — без aiogram, чтобы клавиатуры, тексты и хэндлеры
брали правила из одного места.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stepper:
    """Правила одного числового поля настроек."""

    field: str
    title: str
    hint: str
    unit: str  # «с», «%» или пустая строка
    low: float
    high: float
    step: float  # шаг кнопок «−/+»
    presets: tuple[float, ...]  # быстрые значения
    integer: bool = False
    back: str = "screen:manual"  # куда ведёт «Готово»

    def clamp(self, value: float) -> float:
        value = max(self.low, min(self.high, value))
        return float(round(value)) if self.integer else round(value, 2)

    def valid(self, value: float) -> bool:
        return self.low <= value <= self.high and (not self.integer or value == int(value))

    def show(self, value: float) -> str:
        """`30.0` → «30 с», `85` → «85%», `7` → «7»."""
        text = f"{value:g}"
        if not self.unit:
            return text
        return f"{text}{self.unit}" if self.unit == "%" else f"{text} {self.unit}"

    def range_text(self) -> str:
        return f"от {self.low:g} до {self.high:g}"


STEPPERS: dict[str, Stepper] = {
    stepper.field: stepper
    for stepper in (
        Stepper(
            "num_clips",
            "Сколько клипов",
            "Столько клипов я вырежу из ролика.",
            "",
            1,
            50,
            1,
            (1, 3, 5, 10, 20, 30),
            integer=True,
        ),
        Stepper(
            "clip_duration",
            "Длина клипа",
            "Сколько секунд видео в одном клипе — без пауз под баннер. Для TikTok и Shorts лучше всего 20–45 с.",
            "с",
            2,
            180,
            5,
            (15, 20, 30, 45, 60, 90),
        ),
        Stepper(
            "start_offset",
            "Пропустить начало ролика",
            "Сколько секунд отрезать в начале: заставку, приветствие, рекламу.",
            "с",
            0,
            3600,
            5,
            (0, 5, 10, 15, 30, 60),
            back="screen:trim",
        ),
        Stepper(
            "end_offset",
            "Пропустить конец ролика",
            "Сколько секунд отрезать в конце: титры, прощание.",
            "с",
            0,
            3600,
            5,
            (0, 5, 10, 15, 30, 60),
            back="screen:trim",
        ),
        Stepper(
            "banner_count",
            "Сколько раз показать баннер",
            "Столько раз баннер появится в одном клипе. Каждый показ с паузой добавляет к клипу длину баннера.",
            "",
            1,
            5,
            1,
            (1, 2, 3),
            integer=True,
        ),
        Stepper(
            "banner_scale",
            "Размер баннера",
            "Какую часть ширины кадра займёт баннер.",
            "%",
            50,
            95,
            5,
            (60, 70, 80, 85, 90, 95),
            integer=True,
        ),
    )
}
