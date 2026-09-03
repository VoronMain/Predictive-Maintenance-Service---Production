# -*- coding: utf-8 -*-
"""
pipeline.py — оркестратор конвейера обработки данных СПА.

Реализует слоистую организацию обработки: сбор → валидация →
буферизация → агрегация → формирование признаков → ML-инференс →
сохранение в базу данных → детектирование инцидентов и оповещения.
Каждый этап вынесен в отдельный модуль, что обеспечивает независимое
тестирование и наглядность пайплайна.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .buffer import AggregationManager
from .db_base import DatabaseProtocol
from .features import build_feature_vector
from .incidents import IncidentDetector, IncidentEvent
from .ml_service import MLService
from .notifications import NotificationService
from .schema import EquipmentRecord, PredictionRecord, TelemetryMeasurement

log = logging.getLogger(__name__)


class Pipeline:
    """Сквозной конвейер обработки измерения от приёма до сохранения.

    Поток данных проходит однонаправленно: входной обработчик
    выполняет валидацию, помещает измерение в кольцевой буфер,
    формирует агрегат и вектор признаков, передаёт его в ML-сервис,
    сохраняет полученный предикт в базу данных и при необходимости
    инициирует подсистему оповещений.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        aggregator: AggregationManager,
        ml_service: MLService,
        incident_detector: Optional[IncidentDetector] = None,
        notifier: Optional[NotificationService] = None,
        min_samples_for_inference: int = 1,
    ) -> None:
        self.db = db
        self.aggregator = aggregator
        self.ml = ml_service
        self.incidents = incident_detector
        self.notifier = notifier
        self.min_samples_for_inference = min_samples_for_inference

    def ingest(self, measurement: TelemetryMeasurement) -> Optional[PredictionRecord]:
        """Обрабатывает одно входящее измерение и возвращает предикт.

        Returns
        -------
        PredictionRecord | None
            Запись предсказания, если выполнен инференс. None при
            недостаточном количестве измерений в буфере.
        """
        # Регистрация единицы оборудования в справочнике. Наработку
        # передаём из измерения: иначе ON CONFLICT-обновление затёрло бы
        # значение справочника нулём по умолчанию EquipmentRecord, что
        # сломало бы определение статуса агрегата (наработка < 1000 ч → новое).
        self.db.upsert_equipment(
            EquipmentRecord(
                machine_id=measurement.machine_id,
                machine_type=measurement.machine_type,
                operational_hours=measurement.operational_hours,
            )
        )
        # Сохранение сырого измерения.
        self.db.insert_raw_measurement(measurement)
        # Помещение в кольцевой буфер.
        self.aggregator.append(measurement)

        # Формирование агрегата и вектора признаков.
        aggregate = self.aggregator.aggregate(measurement.machine_id)
        if aggregate is None or aggregate["n_samples"] < self.min_samples_for_inference:
            return None

        features = build_feature_vector(aggregate)
        # Сохранение часового агрегата для последующего ретроспективного
        # анализа и мониторинга качества моделей.
        self.db.insert_hourly_aggregate(
            machine_id=measurement.machine_id,
            window_end=aggregate["window_end"],
            features=features.iloc[0].to_dict(),
        )

        # Запуск ML-инференса.
        prediction = self.ml.predict(
            features=features,
            machine_id=measurement.machine_id,
            timestamp=measurement.timestamp,
        )
        self.db.insert_prediction(prediction)
        log.info(
            "Инференс по %s: p=%.3f label=%d RUL=%.1f дн.",
            prediction.machine_id,
            prediction.failure_probability,
            prediction.failure_label,
            prediction.remaining_useful_life_days,
        )

        # Детектирование инцидента и формирование оповещения.
        self._handle_incident(prediction, measurement.machine_type)
        return prediction

    def _handle_incident(self, prediction: PredictionRecord,
                          machine_type: str) -> None:
        if self.incidents is None:
            return
        result = self.incidents.process(prediction)
        if self.notifier is None:
            return
        if result.event == IncidentEvent.OPENED:
            # Принудительная отправка при открытии инцидента —
            # окно подавления повторов в этом случае игнорируется.
            self.notifier.notify(
                prediction=prediction,
                machine_type=machine_type,
                incident_id=result.incident_id,
                force=True,
            )
        elif result.event == IncidentEvent.UPDATED:
            # При продолжающемся инциденте оповещение отправляется
            # повторно только за пределами окна подавления повторов.
            self.notifier.notify(
                prediction=prediction,
                machine_type=machine_type,
                incident_id=result.incident_id,
                force=False,
            )
