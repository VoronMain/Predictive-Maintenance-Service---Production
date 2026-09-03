# -*- coding: utf-8 -*-
"""
schema.py — Pydantic-схемы валидации входящего потока телеметрии
и форматов хранения данных в подсистемах СПА.

Валидация обеспечивает соблюдение типов, диапазонов значений и
наличия обязательных полей. Аппаратные выбросы по нижней границе
ограничиваются операцией обрезки (clipping).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Перечень допустимых типов оборудования соответствует справочнику
# Machine_Type, использованному при обучении ML-моделей.
ALLOWED_MACHINE_TYPES = (
    "3D_Printer",
    "AGV",
    "Automated_Screwdriver",
    "Boiler",
    "CMM",
    "CNC_Lathe",
    "CNC_Mill",
    "Carton_Former",
    "Compressor",
    "Conveyor_Belt",
    "Crane",
    "Dryer",
    "Forklift_Electric",
    "Furnace",
    "Grinder",
    "Heat_Exchanger",
    "Hydraulic_Press",
    "Industrial_Chiller",
    "Injection_Molder",
    "Labeler",
    "Laser_Cutter",
    "Mixer",
    "Palletizer",
    "Pick_and_Place",
    "Press_Brake",
    "Pump",
    "Robot_Arm",
    "Shrink_Wrapper",
    "Shuttle_System",
    "Vacuum_Packer",
    "Valve_Controller",
    "Vision_System",
    "XRay_Inspector",
)


class TelemetryMeasurement(BaseModel):
    """Структура одного входящего измерения от единицы оборудования.

    Аппаратные выбросы по нижней границе (отрицательные значения
    вибрации, мощности и иных физических величин) ограничиваются
    операцией обрезки на этапе валидации.
    """

    model_config = ConfigDict(extra="forbid")

    machine_id: str = Field(..., min_length=1, max_length=64)
    machine_type: str
    timestamp: datetime
    operational_hours: float = Field(..., ge=0)
    temperature_c: float = Field(..., ge=-50, le=200)
    vibration_mms: float = Field(..., ge=0, le=50)
    sound_db: float = Field(..., ge=0, le=140)
    oil_level_pct: float = Field(..., ge=0, le=100)
    coolant_level_pct: float = Field(..., ge=0, le=100)
    power_consumption_kw: float = Field(..., ge=0, le=600)
    last_maintenance_days_ago: int = Field(..., ge=0, le=365)
    maintenance_history_count: int = Field(..., ge=0)
    failure_history_count: int = Field(..., ge=0)
    ai_supervision: bool
    error_codes_last_30_days: int = Field(..., ge=0, le=100)
    ai_override_events: int = Field(..., ge=0)
    # Специфические признаки оборудования (MNAR). Не для всех типов
    # установлены соответствующие датчики, поэтому поле опционально.
    laser_intensity: Optional[float] = None
    hydraulic_pressure_bar: Optional[float] = None
    coolant_flow_l_min: Optional[float] = None
    heat_index: Optional[float] = None

    @field_validator("machine_type")
    @classmethod
    def _validate_machine_type(cls, value: str) -> str:
        if value not in ALLOWED_MACHINE_TYPES:
            raise ValueError(
                f"Недопустимый тип оборудования: {value!r}. "
                f"Ожидается один из {len(ALLOWED_MACHINE_TYPES)} известных типов."
            )
        return value


class PredictionRecord(BaseModel):
    """Структура записи предсказания, сохраняемой в таблице predictions."""

    model_config = ConfigDict(extra="forbid")

    machine_id: str
    timestamp: datetime
    failure_probability: float = Field(..., ge=0.0, le=1.0)
    failure_label: int = Field(..., ge=0, le=1)
    remaining_useful_life_days: float = Field(..., ge=0.0)
    threshold: float = Field(..., ge=0.0, le=1.0)


class EquipmentRecord(BaseModel):
    """Справочная запись о единице оборудования."""

    model_config = ConfigDict(extra="forbid")

    machine_id: str
    machine_type: str
    operational_hours: float = 0.0
    category: str = ""
