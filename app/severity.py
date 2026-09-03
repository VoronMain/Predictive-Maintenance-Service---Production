# -*- coding: utf-8 -*-
"""
severity.py — модуль классификации степени критичности предаварийного
состояния оборудования.

Степень критичности (severity) выводится из магнитуды предсказания
ML-инференса: калиброванной вероятности отказа и оценки остаточного
полезного ресурса (RUL). Полученная степень критичности управляет
двумя аспектами работы подсистемы оповещений:

  * динамическим окном подавления повторов (cooldown) — чем выше
    критичность, тем короче окно и тем чаще допускается повторное
    уведомление персонала;
  * приоритетом и окном групповой агрегации однотипных оповещений —
    оповещения уровня critical доставляются немедленно, а оповещения
    меньшей критичности накапливаются в пределах окна группировки
    и объединяются в одно сводное сообщение.

Принятая шкала степеней критичности (по убыванию приоритета):

  CRITICAL — p ≥ SEVERITY_CRITICAL_PROB либо RUL ≤ SEVERITY_CRITICAL_RUL_DAYS;
  HIGH     — p ≥ SEVERITY_HIGH_PROB     либо RUL ≤ SEVERITY_HIGH_RUL_DAYS;
  MEDIUM   — t* ≤ p < SEVERITY_HIGH_PROB (предаварийное состояние без
             признаков немедленной эскалации);
  NONE     — p < t* (нормальное состояние, оповещение не формируется).
"""
from __future__ import annotations

from enum import Enum

from .config import settings


class Severity(str, Enum):
    """Степень критичности предаварийного состояния оборудования."""

    NONE = "none"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Числовой ранг степени критичности для сопоставления и выбора
        максимума (пиковой критичности инцидента)."""
        return _RANK[self]

    @property
    def label_ru(self) -> str:
        """Человекочитаемое наименование степени критичности."""
        return _LABEL_RU[self]

    def is_alerting(self) -> bool:
        """Признак того, что степень критичности порождает оповещение."""
        return self is not Severity.NONE


_RANK = {
    Severity.NONE: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

_LABEL_RU = {
    Severity.NONE: "норма",
    Severity.MEDIUM: "умеренная",
    Severity.HIGH: "высокая",
    Severity.CRITICAL: "критическая",
}


def classify(probability: float, rul_days: float,
             threshold: float) -> Severity:
    """Классифицирует степень критичности по вероятности отказа и RUL.

    Parameters
    ----------
    probability : float
        Калиброванная вероятность отказа на горизонте семи суток.
    rul_days : float
        Оценка остаточного полезного ресурса в сутках.
    threshold : float
        Порог классификации t*, ниже которого состояние считается
        нормальным.

    Returns
    -------
    Severity
        Степень критичности. Значение NONE соответствует нормальному
        состоянию (p < t*).
    """
    if probability < threshold:
        return Severity.NONE
    if (probability >= settings.SEVERITY_CRITICAL_PROB
            or rul_days <= settings.SEVERITY_CRITICAL_RUL_DAYS):
        return Severity.CRITICAL
    if (probability >= settings.SEVERITY_HIGH_PROB
            or rul_days <= settings.SEVERITY_HIGH_RUL_DAYS):
        return Severity.HIGH
    return Severity.MEDIUM


def cooldown_minutes(severity: Severity) -> int:
    """Возвращает окно подавления повторов (мин.) для степени критичности.

    Чем выше критичность, тем короче окно подавления повторных
    оповещений по тому же инциденту.
    """
    return {
        Severity.CRITICAL: settings.COOLDOWN_CRITICAL_MINUTES,
        Severity.HIGH: settings.COOLDOWN_HIGH_MINUTES,
        Severity.MEDIUM: settings.COOLDOWN_MEDIUM_MINUTES,
        Severity.NONE: settings.ALERT_COOLDOWN_MINUTES,
    }[severity]


def group_window_seconds(severity: Severity) -> int:
    """Возвращает окно групповой агрегации (сек.) для степени критичности.

    Для уровня critical окно обнуляется, что обеспечивает немедленную
    доставку оповещения без ожидания агрегации однотипных событий.
    """
    if severity is Severity.CRITICAL:
        return 0
    return settings.ALERT_GROUP_WINDOW_SECONDS


def severity_from_value(value: str | None) -> Severity:
    """Восстанавливает степень критичности из строкового значения БД."""
    if not value:
        return Severity.NONE
    try:
        return Severity(value)
    except ValueError:
        return Severity.NONE


def max_severity(a: Severity, b: Severity) -> Severity:
    """Возвращает степень критичности с большим рангом."""
    return a if a.rank >= b.rank else b
