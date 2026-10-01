# -*- coding: utf-8 -*-
"""
aggregate_status.py — единственное место, где определяется статус агрегата.

Статус отдаётся сервером во всех ответах, где он нужен (строки обзора
предсказаний, метаданные агрегата, счётчики дашборда); страницы его
только отображают.
"""
from __future__ import annotations

from typing import Optional

STATUS_NEW = "new"
STATUS_NORMAL = "normal"
STATUS_PRE_FAILURE = "pre_failure"

# Агрегат с меньшей наработкой считается новым, даже если у него уже
# есть предсказания.
NEW_MACHINE_HOURS = 1000.0


def aggregate_status(
    operational_hours: Optional[float],
    failure_probability: Optional[float],
    threshold: float,
) -> str:
    """Статус агрегата по наработке, последней вероятности отказа
    (None — предсказаний ещё нет) и действующему порогу t*."""
    if failure_probability is None or (operational_hours or 0.0) < NEW_MACHINE_HOURS:
        return STATUS_NEW
    if failure_probability >= threshold:
        return STATUS_PRE_FAILURE
    return STATUS_NORMAL
