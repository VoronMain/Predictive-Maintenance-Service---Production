# -*- coding: utf-8 -*-
"""
feature_window.py — окно признаков: измерения на входе, вектор признаков на выходе.

Один module вместо пары «буфер агрегации → словарь → формирование
признаков». Он держит окно измерений по каждому агрегату и на каждое
новое измерение отвечает одним из двух результатов: FeatureVector
(вектор из 67 признаков плюс служебные данные окна) или None —
«мало данных». Статистики окна наружу не выходят.

Тестируется через конвейер (app.pipeline.Pipeline): сохранённые часовые
агрегаты содержат весь вектор признаков.

Контракт признаков обученной модели — именованные величины ниже. Они не
связаны с нормами датчиков для интерфейса: совпадение чисел 20 / 30 / 90 / 25
случайное.
"""
from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Dict, Optional

import pandas as pd

from .schema import TelemetryMeasurement
from .utils import _ensure_models_on_path, _isnan

_ensure_models_on_path()
from predictive_maintenance.schema import FEATURE_COLUMNS  # noqa: E402

# Перечень типов оборудования, использующийся для one-hot кодирования.
MACHINE_TYPES = [c.removeprefix("mtype_") for c in FEATURE_COLUMNS
                 if c.startswith("mtype_")]

# ---------------------------------------------------------------------------
# Контракт признаков обученной модели
# ---------------------------------------------------------------------------

DATASET_YEAR = 2040

# Источника возраста агрегата в системе нет: ни в измерении, ни в справочнике
# агрегатов (столбец года установки удалён намеренно), ни в эмуляторе. Пока
# источник не появится, возраст для модели — «неизвестен» = 1 год. Это явное
# решение, а не побочный эффект значения по умолчанию для отсутствующего
# поля. Hours_per_Year, Days_Since_Install, Maint_Freq_days и
# Maintenance_Overdue считаются от этого значения.
AGE_UNKNOWN_YEARS = 1

# Медианы MNAR-признаков по обучающей выборке: подставляются, когда у
# агрегата нет физического датчика (индикатор *_available при этом 0).
MNAR_MEDIANS = {
    "Laser_Intensity": 5000.0,
    "Hydraulic_Pressure_bar": 150.0,
    "Coolant_Flow_L_min": 40.0,
    "Heat_Index": 70.0,
}
_MNAR_SENSORS = {
    "Laser_Intensity": "laser_intensity",
    "Hydraulic_Pressure_bar": "hydraulic_pressure_bar",
    "Coolant_Flow_L_min": "coolant_flow_l_min",
    "Heat_Index": "heat_index",
}

# Пороги производных признаков (feature engineering обучающего датасета).
HIGH_VIBRATION_MMS = 20
LOW_OIL_PCT = 30
HIGH_TEMPERATURE_C = 90
LOW_COOLANT_PCT = 25
# Просрочка ТО: дней с последнего ТО > порог × средний интервал между ТО.
OVERDUE_THRESHOLD = 1.5

# Числовые поля измерения, по которым считаются средние окна.
_NUMERIC_FIELDS = (
    "temperature_c",
    "vibration_mms",
    "sound_db",
    "oil_level_pct",
    "coolant_level_pct",
    "power_consumption_kw",
    "operational_hours",
    "last_maintenance_days_ago",
    "error_codes_last_30_days",
    "ai_override_events",
    "laser_intensity",
    "hydraulic_pressure_bar",
    "coolant_flow_l_min",
    "heat_index",
)


@dataclass(frozen=True)
class FeatureVector:
    """Результат «вектор признаков готов»."""

    features: pd.DataFrame  # одна строка, 67 признаков в порядке FEATURE_COLUMNS
    n_samples: int          # число измерений в окне
    window_end: datetime    # время последнего измерения окна


class FeatureWindow:
    """Окно измерений по каждому агрегату и вектор признаков на его выходе.

    Параметры
    ---------
    window_seconds : длина окна; измерения старше неё вытесняются.
    min_samples : минимальное число измерений в окне, при меньшем add()
        возвращает None («мало данных»).

    Потокобезопасно: у каждого агрегата свой буфер со своей блокировкой.
    """

    def __init__(self, window_seconds: int, min_samples: int = 1) -> None:
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self._window = timedelta(seconds=window_seconds)
        self._buffers: Dict[str, Deque[TelemetryMeasurement]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def add(self, measurement: TelemetryMeasurement) -> Optional[FeatureVector]:
        """Принимает измерение; возвращает вектор признаков или None."""
        items = self._push(measurement)
        if len(items) < self.min_samples:
            return None
        return FeatureVector(
            features=_build_features(_window_means(items), items[-1]),
            n_samples=len(items),
            window_end=items[-1].timestamp,
        )

    def _push(self, measurement: TelemetryMeasurement) -> list[TelemetryMeasurement]:
        machine_id = measurement.machine_id
        with self._registry_lock:
            buf = self._buffers.get(machine_id)
            if buf is None:
                buf = self._buffers[machine_id] = deque()
                self._locks[machine_id] = threading.Lock()
            lock = self._locks[machine_id]
        with lock:
            buf.append(measurement)
            cutoff = measurement.timestamp - self._window
            while buf and buf[0].timestamp < cutoff:
                buf.popleft()
            return list(buf)


def _window_means(items: list[TelemetryMeasurement]) -> dict[str, float]:
    """Среднее по каждому числовому полю окна; NaN, если значений нет."""
    means: dict[str, float] = {}
    for field in _NUMERIC_FIELDS:
        values = [v for v in (getattr(m, field) for m in items) if not _isnan(v)]
        means[field] = statistics.fmean(values) if values else math.nan
    return means


def _build_features(means: dict[str, float], last: TelemetryMeasurement) -> pd.DataFrame:
    """Собирает вектор из средних окна и статических полей последнего измерения."""
    record: dict = {}

    # Базовые сенсорные признаки — средние за окно.
    record["Operational_Hours"] = means["operational_hours"]
    record["Temperature_C"] = means["temperature_c"]
    record["Vibration_mms"] = means["vibration_mms"]
    record["Sound_dB"] = means["sound_db"]
    record["Oil_Level_pct"] = means["oil_level_pct"]
    record["Coolant_Level_pct"] = means["coolant_level_pct"]
    record["Power_Consumption_kW"] = means["power_consumption_kw"]
    record["Last_Maintenance_Days_Ago"] = means["last_maintenance_days_ago"]
    record["Maintenance_History_Count"] = last.maintenance_history_count
    record["Failure_History_Count"] = last.failure_history_count
    record["AI_Supervision"] = int(last.ai_supervision)
    record["Error_Codes_Last_30_Days"] = means["error_codes_last_30_days"]
    record["AI_Override_Events"] = means["ai_override_events"]

    # MNAR-признаки: нет датчика → медиана и индикатор наличия 0.
    for feat, field in _MNAR_SENSORS.items():
        value = means[field]
        if _isnan(value):
            record[feat] = MNAR_MEDIANS[feat]
            record[f"{feat}_available"] = 0
        else:
            record[feat] = float(value)
            record[f"{feat}_available"] = 1

    # One-hot кодирование типа оборудования (неизвестный тип отвергает
    # валидация измерения на входе).
    for mt in MACHINE_TYPES:
        record[f"mtype_{mt}"] = 1 if mt == last.machine_type else 0

    # Производные признаки.
    age_years = AGE_UNKNOWN_YEARS
    record["Machine_Age_years"] = age_years
    record["Hours_per_Year"] = record["Operational_Hours"] / age_years
    record["Stress_Index"] = record["Temperature_C"] * record["Vibration_mms"]
    record["Fluid_Score"] = record["Oil_Level_pct"] + record["Coolant_Level_pct"]
    record["Maintenance_Urgency"] = record["Last_Maintenance_Days_Ago"] * (
        record["Failure_History_Count"] + 1
    )
    record["Error_Rate"] = record["Error_Codes_Last_30_Days"] / (
        record["Operational_Hours"] / 720 + 1
    )
    record["High_Vibration"] = int(record["Vibration_mms"] > HIGH_VIBRATION_MMS)
    record["Low_Oil"] = int(record["Oil_Level_pct"] < LOW_OIL_PCT)
    record["High_Temperature"] = int(record["Temperature_C"] > HIGH_TEMPERATURE_C)
    record["Low_Coolant"] = int(record["Coolant_Level_pct"] < LOW_COOLANT_PCT)
    record["Days_Since_Install"] = age_years * 365
    record["Maint_Freq_days"] = record["Days_Since_Install"] / (
        record["Maintenance_History_Count"] + 1
    )
    record["Maintenance_Overdue"] = int(
        record["Last_Maintenance_Days_Ago"]
        > OVERDUE_THRESHOLD * record["Maint_Freq_days"]
    )

    # Контроль наличия всех ожидаемых признаков.
    missing = set(FEATURE_COLUMNS) - set(record.keys())
    if missing:
        raise ValueError(
            f"Внутренняя ошибка формирования вектора признаков: "
            f"не заполнены поля {sorted(missing)}"
        )
    return pd.DataFrame([record])[list(FEATURE_COLUMNS)]
