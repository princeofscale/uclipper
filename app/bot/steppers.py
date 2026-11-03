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
    unit: str  # «с» или пустая строка
    low: float
    high: float
    step: float  # шаг кнопок «−/+»
    presets: tuple[float, ...]  # быстрые значения
    integer: bool = False
    back: str = "screen:settings"  # куда ведёт «Готово»

    def clamp(self, value: float) -> float:
        value = max(self.low, min(self.high, value))
        return float(round(value)) if self.integer else round(value, 2)

    def valid(self, value: float) -> bool:
        return self.low <= value <= self.high and (not self.integer or value == int(value))

    def show(self, value: float) -> str:
        """`30.0` → «30 с», `7` → «7»."""
        text = f"{value:g}"
        return f"{text} {self.unit}" if self.unit else text

    def range_text(self) -> str:
        return f"от {self.low:g} до {self.high:g}"


STEPPERS: dict[str, Stepper] = {
    stepper.field: stepper
    for stepper in (
        Stepper(
            "num_clips",
            "Количество клипов",
            "Сколько клипов вырезать из ролика.",
            "",
            1,
            50,
            1,
            (1, 3, 5, 10, 20, 30),
            integer=True,
        ),
        Stepper(
            "clip_duration",
            "Длительность клипа",
            "Чистая длительность одного клипа — без пауз под баннер.",
            "с",
            2,
            180,
            5,
            (15, 20, 30, 45, 60, 90),
        ),
        Stepper(
            "start_offset",
            "Обрезать начало",
            "Сколько секунд убрать в начале ролика.",
            "с",
            0,
            3600,
            5,
            (0, 5, 10, 15, 30, 60),
            back="screen:trim",
        ),
        Stepper(
            "end_offset",
            "Обрезать конец",
            "Сколько секунд убрать в конце ролика.",
            "с",
            0,
            3600,
            5,
            (0, 5, 10, 15, 30, 60),
            back="screen:trim",
        ),
    )
}
