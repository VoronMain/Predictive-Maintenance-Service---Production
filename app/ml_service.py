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
        failure = self.model.predict_failure(features, threshold=self.threshold)
        rul = self.model.predict_rul(features)
        return PredictionRecord(
            machine_id=machine_id,
            timestamp=timestamp,
            failure_probability=float(failure.probability[0]),
            failure_label=int(failure.label[0]),
            remaining_useful_life_days=float(rul.rul_days[0]),
            threshold=self.threshold,
        )

    def info(self) -> dict:
        return self.model.info()
