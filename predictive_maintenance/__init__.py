# -*- coding: utf-8 -*-
"""
predictive_maintenance — пакет для интеграции моделей предиктивного
обслуживания промышленного оборудования.

Основной интерфейс — класс PredictiveMaintenanceModel.

Пример использования:
    from predictive_maintenance import PredictiveMaintenanceModel

    model = PredictiveMaintenanceModel.load("models/")
    failure_proba, failure_pred = model.predict_failure(df)
    rul_days = model.predict_rul(df)
"""
from .inference import PredictiveMaintenanceModel
from .schema import FEATURE_COLUMNS, MNAR_COLUMNS

__version__ = "1.0.0"
__all__ = ["PredictiveMaintenanceModel", "FEATURE_COLUMNS", "MNAR_COLUMNS"]
