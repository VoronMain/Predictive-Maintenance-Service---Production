# -*- coding: utf-8 -*-
"""
incidents.py — модуль детектирования инцидентов.

Реализует логику открытия и закрытия записей электронного журнала
отказов на основании результатов работы ML-инференса. Инцидент
открывается в момент перехода единицы оборудования через порог
классификации t* (нормальное состояние → предаварийное) и
закрывается при возврате в нормальное состояние. На протяжении
открытого инцидента производится агрегация показателей: фиксируется
максимально достигнутая вероятность отказа, минимальное значение
остаточного полезного ресурса и пиковая степень критичности.

Степень критичности (severity) определяется по магнитуде предсказания
средствами модуля app/severity.py и используется подсистемой
оповещений для динамического подавления повторов и приоритезации.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .db_base import DatabaseProtocol
from .schema import PredictionRecord
from .severity import Severity, classify, max_severity, severity_from_value

log = logging.getLogger(__name__)


class IncidentEvent(str, Enum):
    """Тип события, сопровождающего обновление журнала отказов."""

    OPENED = "opened"
    UPDATED = "updated"
    CLOSED = "closed"
    NONE = "none"


@dataclass
class IncidentResult:
    """Результат обработки одного предсказания детектором инцидентов."""

    event: IncidentEvent
    incident_id: Optional[int] = None
    severity: Severity = Severity.NONE


class IncidentDetector:
    """Сервис управления электронным журналом отказов.

    Анализирует поступающие предсказания и формирует записи в
    таблице incidents_log. Логика основана на принципе «каждый
    переход через порог t* — отдельный инцидент»: новый инцидент
    открывается при переходе нормального состояния в предаварийное;
    инцидент закрывается при возврате метки в значение 0.
    """

    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db

    def process(self, prediction: PredictionRecord) -> IncidentResult:
        """Обрабатывает одно предсказание и обновляет журнал отказов."""
        severity = classify(
            prediction.failure_probability,
            prediction.remaining_useful_life_days,
            prediction.threshold,
        )
        open_incident = self.db.get_open_incident(prediction.machine_id)

        if prediction.failure_label == 1:
            if open_incident is None:
                incident_id = self.db.open_incident(
                    machine_id=prediction.machine_id,
                    opened_at=prediction.timestamp,
                    probability=prediction.failure_probability,
                    rul_days=prediction.remaining_useful_life_days,
                    threshold=prediction.threshold,
                    severity=severity.value,
                )
                log.info(
                    "Открыт инцидент #%d по %s (severity=%s, p=%.3f, RUL=%.1f дн.)",
                    incident_id, prediction.machine_id, severity.value,
                    prediction.failure_probability,
                    prediction.remaining_useful_life_days,
                )
                return IncidentResult(IncidentEvent.OPENED, incident_id, severity)
            peak = max_severity(
                severity_from_value(open_incident.get("peak_severity")), severity
            )
            self.db.update_open_incident(
                incident_id=open_incident["id"],
                probability=prediction.failure_probability,
                rul_days=prediction.remaining_useful_life_days,
                peak_severity=peak.value,
            )
            return IncidentResult(IncidentEvent.UPDATED, open_incident["id"], severity)

        if open_incident is not None:
            self.db.close_incident(open_incident["id"], prediction.timestamp)
            log.info(
                "Закрыт инцидент #%d по %s (возврат в норму)",
                open_incident["id"], prediction.machine_id,
            )
            return IncidentResult(IncidentEvent.CLOSED, open_incident["id"], Severity.NONE)

        return IncidentResult(IncidentEvent.NONE, None, Severity.NONE)
