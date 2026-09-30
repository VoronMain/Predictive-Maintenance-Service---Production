# -*- coding: utf-8 -*-
"""
ml_service.py — обёртка над пакетом predictive_maintenance, реализующая
последовательный ML-инференс для нужд СПА.

Подсистема загружает обученные модели в оперативную память при старте
приложения, чем обеспечивается минимальное время отклика при обработке
очередного вектора признаков. Конвейер инференса последовательно
применяет бинарный классификатор LightGBM с изотонической калибровкой
и регрессионную модель CatBoost для оценки остаточного ресурса.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from .utils import _ensure_models_on_path

_ensure_models_on_path()
from predictive_maintenance import PredictiveMaintenanceModel  # noqa: E402

from .schema import PredictionRecord

log = logging.getLogger(__name__)


class MLService:
    """Сервис ML-инференса СПА.

    Загружает артефакты модели единожды при инициализации и
    предоставляет методы для совмещённого и раздельного инференса.
    """

    def __init__(self, model: PredictiveMaintenanceModel,
                 threshold: Optional[float] = None) -> None:
        self.model = model
        self.threshold = threshold if threshold is not None else model.threshold

    @classmethod
    def from_path(cls, models_dir: Path,
                  threshold: Optional[float] = None) -> "MLService":
        log.info("Загрузка ML-моделей из %s", models_dir)
        model = PredictiveMaintenanceModel.load(models_dir)
        return cls(model=model, threshold=threshold)

    def predict(self, features: pd.DataFrame, machine_id: str,
                timestamp: datetime) -> PredictionRecord:
        """Выполняет совмещённый инференс и возвращает запись предсказания."""
        return self.predict_batch(features, [machine_id], [timestamp])[0]

    def predict_batch(self, features: pd.DataFrame, machine_ids: list[str],
                      timestamps: list[datetime]) -> list[PredictionRecord]:
        """Совмещённый инференс по пачке векторов признаков.

        Строка ``features[i]`` относится к агрегату ``machine_ids[i]`` на
        момент ``timestamps[i]``. Одиночный predict() — частный случай
        этого метода, поэтому порог и формирование записи предсказания
        одинаковы для потока и для истории.
        """
        failure = self.model.predict_failure(features, threshold=self.threshold)
        rul = self.model.predict_rul(features)
        return [
            PredictionRecord(
                machine_id=machine_id,
                timestamp=timestamp,
                failure_probability=float(failure.probability[i]),
                failure_label=int(failure.label[i]),
                remaining_useful_life_days=float(rul.rul_days[i]),
                threshold=self.threshold,
            )
            for i, (machine_id, timestamp) in enumerate(zip(machine_ids, timestamps))
        ]

    def info(self) -> dict:
        return self.model.info()
