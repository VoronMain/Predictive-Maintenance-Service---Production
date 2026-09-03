# -*- coding: utf-8 -*-
"""
inference.py — основной интерфейс инференса моделей предиктивного
обслуживания. Экспортирует класс PredictiveMaintenanceModel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from .preprocessing import restore_mnar_for_regression
from .schema import FEATURE_COLUMNS, validate_features


@dataclass
class FailurePrediction:
    """Результат прогноза отказа оборудования в горизонте 7 суток.

    Attributes
    ----------
    probability : numpy.ndarray
        Калиброванные вероятности положительного класса в диапазоне [0, 1].
    label : numpy.ndarray
        Бинарные метки (0/1), полученные применением оптимального порога t*.
    threshold : float
        Использованное значение порога классификации.
    """
    probability: np.ndarray
    label: np.ndarray
    threshold: float


@dataclass
class RULPrediction:
    """Результат прогноза остаточного ресурса оборудования.

    Attributes
    ----------
    rul_days : numpy.ndarray
        Прогноз остаточного ресурса в сутках, обрезанный снизу нулём.
    rmse_holdout : float
        Среднеквадратическая ошибка модели на отложенной тестовой
        выборке, рассчитанная при обучении.
    """
    rul_days: np.ndarray
    rmse_holdout: float


class PredictiveMaintenanceModel:
    """Объединённая модель предиктивного обслуживания промышленного
    оборудования.

    Обеспечивает две задачи:

    * Бинарная классификация Failure_Within_7_Days с использованием
      LightGBM, изотонической калибровки вероятностей и оптимального
      порога t*, найденного по критерию максимума F1-меры.
    * Регрессия остаточного ресурса Remaining_Useful_Life_days
      с использованием CatBoost в режиме nan_mode='Min'.

    Метод load() загружает все артефакты из указанной директории,
    методы predict_failure() и predict_rul() выполняют инференс.

    Пример использования
    --------------------
    >>> import pandas as pd
    >>> from predictive_maintenance import PredictiveMaintenanceModel
    >>> model = PredictiveMaintenanceModel.load("models/")
    >>> df = pd.read_parquet("new_data.parquet")
    >>> failure = model.predict_failure(df)
    >>> rul = model.predict_rul(df)
    """

    # ---------------- инициализация ---------------- #
    def __init__(
        self,
        lightgbm_classifier,
        isotonic_calibrator,
        catboost_regressor: CatBoostRegressor,
        threshold: float,
        config: dict,
    ) -> None:
        self.lightgbm = lightgbm_classifier
        self.calibrator = isotonic_calibrator
        self.catboost = catboost_regressor
        self.threshold = float(threshold)
        self.config = config
        self._feature_columns = FEATURE_COLUMNS

    # ---------------- сериализация ---------------- #
    @classmethod
    def load(cls, path: Union[str, Path]) -> "PredictiveMaintenanceModel":
        """Загружает обученную модель из директории с артефактами.

        Parameters
        ----------
        path : str | pathlib.Path
            Путь к директории, содержащей файлы:
              * lightgbm_classifier.joblib
              * isotonic_calibrator.joblib
              * catboost_regressor.cbm
              * model_config.json

        Returns
        -------
        PredictiveMaintenanceModel
            Готовый к инференсу экземпляр модели.
        """
        path = Path(path)
        lgb = joblib.load(path / "lightgbm_classifier.joblib")
        iso = joblib.load(path / "isotonic_calibrator.joblib")
        cb = CatBoostRegressor()
        cb.load_model(str(path / "catboost_regressor.cbm"))
        with open(path / "model_config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cls(
            lightgbm_classifier=lgb,
            isotonic_calibrator=iso,
            catboost_regressor=cb,
            threshold=cfg["classification"]["optimal_threshold"],
            config=cfg,
        )

    def save(self, path: Union[str, Path]) -> None:
        """Сохраняет все артефакты модели в указанную директорию."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.lightgbm, path / "lightgbm_classifier.joblib")
        joblib.dump(self.calibrator, path / "isotonic_calibrator.joblib")
        self.catboost.save_model(str(path / "catboost_regressor.cbm"))
        with open(path / "model_config.json", "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, ensure_ascii=False, indent=2)

    # ---------------- инференс ---------------- #
    def predict_failure_proba(self, df: pd.DataFrame) -> np.ndarray:
        """Возвращает калиброванные вероятности класса 'Отказ в 7 суток'."""
        X = validate_features(df)
        raw = self.lightgbm.predict_proba(X)[:, 1]
        calibrated = self.calibrator.transform(raw)
        return np.asarray(calibrated, dtype=np.float64)

    def predict_failure(
        self, df: pd.DataFrame, threshold: Optional[float] = None
    ) -> FailurePrediction:
        """Прогнозирует факт отказа оборудования в горизонте 7 суток.

        Parameters
        ----------
        df : pandas.DataFrame
            Фрейм признаков. Должен содержать все 67 столбцов
            из FEATURE_COLUMNS.
        threshold : float | None
            Пользовательский порог классификации. Если None,
            используется оптимальный порог t*, найденный при обучении.

        Returns
        -------
        FailurePrediction
            Объект с полями probability, label, threshold.
        """
        proba = self.predict_failure_proba(df)
        t = self.threshold if threshold is None else float(threshold)
        label = (proba >= t).astype(np.int8)
        return FailurePrediction(probability=proba, label=label, threshold=t)

    def predict_rul(self, df: pd.DataFrame) -> RULPrediction:
        """Прогнозирует остаточный ресурс оборудования (RUL) в сутках.

        Перед инференсом восстанавливает NaN в MNAR-столбцах
        в соответствии с обученным режимом nan_mode='Min'.

        Parameters
        ----------
        df : pandas.DataFrame
            Фрейм признаков. Должен содержать все 67 столбцов
            из FEATURE_COLUMNS.

        Returns
        -------
        RULPrediction
            Объект с прогнозом RUL (дн.) и контрольным значением RMSE.
        """
        X = validate_features(df)
        X = restore_mnar_for_regression(X)
        pred = self.catboost.predict(X)
        pred = np.clip(pred, 0.0, None)
        rmse = float(self.config.get("regression", {}).get("rmse", float("nan")))
        return RULPrediction(rul_days=pred, rmse_holdout=rmse)

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        """Возвращает оба прогноза в виде единого фрейма-результата.

        Returns
        -------
        pandas.DataFrame
            Колонки: failure_probability, failure_label,
            remaining_useful_life_days.
        """
        f = self.predict_failure(df)
        r = self.predict_rul(df)
        return pd.DataFrame(
            {
                "failure_probability": f.probability,
                "failure_label": f.label,
                "remaining_useful_life_days": r.rul_days,
            },
            index=df.index,
        )

    # ---------------- сведения о модели ---------------- #
    def info(self) -> dict:
        """Возвращает сводную информацию о модели и обученных метриках."""
        clf = self.config.get("classification", {})
        reg = self.config.get("regression", {})
        return {
            "version": self.config.get("version", "1.0.0"),
            "n_features": len(self._feature_columns),
            "classification": {
                "algorithm": "LightGBM + Isotonic + Threshold Moving",
                "threshold": self.threshold,
                "f1": clf.get("lightgbm_tuned", {}).get("f1"),
                "roc_auc": clf.get("lightgbm_tuned", {}).get("roc_auc"),
            },
            "regression": {
                "algorithm": "CatBoost (nan_mode='Min', RMSE)",
                "rmse": reg.get("rmse"),
                "mae": reg.get("mae"),
                "r2": reg.get("r2"),
            },
        }
