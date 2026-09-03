# -*- coding: utf-8 -*-
"""
features.py — модуль формирования вектора признаков ML-инференса.

Преобразует агрегированный вектор измерений в стандартизированный
вектор из 67 признаков, требуемый ML-моделями. Состав признаков
соответствует перечню FEATURE_COLUMNS пакета predictive_maintenance.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import pandas as pd

from .utils import _ensure_models_on_path, _isnan

_ensure_models_on_path()
from predictive_maintenance.schema import FEATURE_COLUMNS, MNAR_COLUMNS  # noqa: E402

# Перечень типов оборудования, использующийся для one-hot кодирования.
MACHINE_TYPES = [c.removeprefix("mtype_") for c in FEATURE_COLUMNS
                 if c.startswith("mtype_")]

DATASET_YEAR = 2040
OVERDUE_THRESHOLD = 1.5


def build_feature_vector(aggregate: dict) -> pd.DataFrame:
    """Формирует вектор признаков по агрегированным измерениям.

    Parameters
    ----------
    aggregate : dict
        Агрегированный вектор измерений, сформированный модулем
        agregation.AggregationManager.aggregate().

    Returns
    -------
    pandas.DataFrame
        Фрейм из одной строки с 67 признаками, упорядоченными
        в соответствии с FEATURE_COLUMNS.
    """
    record: dict = {}

    # Базовые сенсорные признаки заполняются средними значениями
    # за окно агрегации.
    record["Operational_Hours"] = aggregate.get("operational_hours_mean", 0.0)
    record["Temperature_C"] = aggregate.get("temperature_c_mean", 0.0)
    record["Vibration_mms"] = aggregate.get("vibration_mms_mean", 0.0)
    record["Sound_dB"] = aggregate.get("sound_db_mean", 0.0)
    record["Oil_Level_pct"] = aggregate.get("oil_level_pct_mean", 0.0)
    record["Coolant_Level_pct"] = aggregate.get("coolant_level_pct_mean", 0.0)
    record["Power_Consumption_kW"] = aggregate.get("power_consumption_kw_mean", 0.0)
    record["Last_Maintenance_Days_Ago"] = aggregate.get(
        "last_maintenance_days_ago_mean", 0.0
    )
    record["Maintenance_History_Count"] = aggregate.get(
        "maintenance_history_count", 0
    )
    record["Failure_History_Count"] = aggregate.get("failure_history_count", 0)
    record["AI_Supervision"] = aggregate.get("ai_supervision", 0)
    record["Error_Codes_Last_30_Days"] = aggregate.get(
        "error_codes_last_30_days_mean", 0.0
    )
    record["AI_Override_Events"] = aggregate.get(
        "ai_override_events_mean", 0.0
    )

    # MNAR-признаки: при отсутствии физического датчика среднее
    # за окно содержит NaN; в исходной модели обучения такие
    # значения заполнялись медианой по выборке. На этапе инференса
    # CatBoost получает NaN, индикатор *_available сбрасывается в 0.
    medians = {
        "Laser_Intensity": 5000.0,
        "Hydraulic_Pressure_bar": 150.0,
        "Coolant_Flow_L_min": 40.0,
        "Heat_Index": 70.0,
    }
    sensor_map = {
        "Laser_Intensity": "laser_intensity_mean",
        "Hydraulic_Pressure_bar": "hydraulic_pressure_bar_mean",
        "Coolant_Flow_L_min": "coolant_flow_l_min_mean",
        "Heat_Index": "heat_index_mean",
    }
    for feat, agg_key in sensor_map.items():
        value = aggregate.get(agg_key)
        if _isnan(value):
            record[feat] = medians[feat]
            record[f"{feat}_available"] = 0
        else:
            record[feat] = float(value)
            record[f"{feat}_available"] = 1

    # One-hot кодирование типа оборудования.
    machine_type = aggregate.get("machine_type")
    for mt in MACHINE_TYPES:
        record[f"mtype_{mt}"] = 1 if mt == machine_type else 0

    # Производные признаки, повторяющие feature engineering
    # из этапа предобработки обучающего датасета.
    age_years = max(DATASET_YEAR - int(aggregate.get("installation_year", DATASET_YEAR)), 1)
    record["Machine_Age_years"] = age_years
    record["Hours_per_Year"] = record["Operational_Hours"] / max(age_years, 1)
    record["Stress_Index"] = record["Temperature_C"] * record["Vibration_mms"]
    record["Fluid_Score"] = record["Oil_Level_pct"] + record["Coolant_Level_pct"]
    record["Maintenance_Urgency"] = record["Last_Maintenance_Days_Ago"] * (
        record["Failure_History_Count"] + 1
    )
    record["Error_Rate"] = record["Error_Codes_Last_30_Days"] / (
        record["Operational_Hours"] / 720 + 1
    )
    record["High_Vibration"] = int(record["Vibration_mms"] > 20)
    record["Low_Oil"] = int(record["Oil_Level_pct"] < 30)
    record["High_Temperature"] = int(record["Temperature_C"] > 90)
    record["Low_Coolant"] = int(record["Coolant_Level_pct"] < 25)
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

    frame = pd.DataFrame([record])[list(FEATURE_COLUMNS)]
    return frame
