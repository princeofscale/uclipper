"""Состояния диалога с ботом."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    """Один общий сценарий: в состоянии `editing` лежит черновик задачи (источник + настройки)."""

    waiting_link = State()  # ждём ссылку на ролик
    waiting_query = State()  # ждём название для поиска
    waiting_file = State()  # ждём видеофайл
    choosing = State()  # показываем результаты поиска или трендов, ждём выбора
    editing = State()  # ролик принят: выбор режима, авто-план или ручные настройки
    running = State()  # задача выполняется (диалог заблокирован, пока идёт нарезка)
