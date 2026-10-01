# -*- coding: utf-8 -*-
"""
forge_machines.py — справочник агрегатов кузнечно-прессового цеха.

Содержит 30 единиц оборудования, разделённых на четыре категории:
Прессы, Молоты, Нагревательное оборудование, Вспомогательное оборудование.
Читается module траектории агрегата (trajectory.py), засевом (seeder.py)
и эмулятором потока (emulator/forge_stream.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

# Окно засева исторических данных в днях. Согласовано с вызовом
# seed_historical_data в app/main.py и продолжением тренда деградации
# в эмуляторе потока (emulator/forge_stream.py); прогресс t нормируется на него
# в app/trajectory.py.
SEED_HISTORY_DAYS = 14


@dataclass
class ForgeMachine:
    """Описание одного агрегата кузнечно-прессового цеха."""

    machine_id: str
    display_name: str
    ml_type: str             # один из ALLOWED_MACHINE_TYPES
    category: str
    state: str               # "pre_failure" | "normal" | "new"
    operational_hours: float
    maintenance_history_count: int
    failure_history_count: int
    history_days: int        # глубина исторических данных при засеве


# Базовые профили сенсоров по состоянию.
# Значения подобраны так, чтобы ML-модель генерировала реалистичные
# вероятности: pre_failure — все триггеры High_Vibration, High_Temperature,
# Low_Oil, Low_Coolant; normal и new — значения в норме.
SENSOR_PROFILES: Dict[str, dict] = {
    "pre_failure": {
        "temperature_c": 84.0,
        "vibration_mms": 19.0,
        "sound_db": 102.0,
        "oil_level_pct": 32.0,
        "coolant_level_pct": 27.0,
        "power_consumption_kw": 410.0,
        "last_maintenance_days_ago": 105,
        # Число кодов ошибок намеренно невелико: в обученной модели
        # высокий счётчик кодов коррелирует с ПОНИЖЕННЫМ риском отказа
        # (оборудование под активным наблюдением и обслуживанием), поэтому
        # его рост подавлял бы расчётную вероятность. Риск формируется
        # наработкой (Operational_Hours), а не кодами ошибок.
        "error_codes_last_30_days": 3,
        "ai_override_events": 4,
    },
    "normal": {
        "temperature_c": 65.0,
        "vibration_mms": 11.0,
        "sound_db": 93.0,
        "oil_level_pct": 65.0,
        "coolant_level_pct": 70.0,
        "power_consumption_kw": 270.0,
        "last_maintenance_days_ago": 30,
        "error_codes_last_30_days": 2,
        "ai_override_events": 1,
    },
    "new": {
        "temperature_c": 52.0,
        "vibration_mms": 5.0,
        "sound_db": 84.0,
        "oil_level_pct": 90.0,
        "coolant_level_pct": 88.0,
        "power_consumption_kw": 150.0,
        "last_maintenance_days_ago": 3,
        "error_codes_last_30_days": 0,
        "ai_override_events": 0,
    },
}


FORGE_MACHINES: list[ForgeMachine] = [
    # ------------------------------------------------------------------ #
    # Прессы (12 единиц)
    # ------------------------------------------------------------------ #
    ForgeMachine(
        machine_id="КГШП-001",
        display_name="Кривошипный горячештамповочный пресс №1",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="pre_failure",
        operational_hours=96_000.0,
        maintenance_history_count=12,
        failure_history_count=4,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="КГШП-002",
        display_name="Кривошипный горячештамповочный пресс №2",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="pre_failure",
        operational_hours=97_500.0,
        maintenance_history_count=14,
        failure_history_count=5,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="КГШП-003",
        display_name="Кривошипный горячештамповочный пресс №3",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="normal",
        operational_hours=30_000.0,
        maintenance_history_count=8,
        failure_history_count=1,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="КГШП-004",
        display_name="Кривошипный горячештамповочный пресс №4",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="normal",
        operational_hours=24_000.0,
        maintenance_history_count=6,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ГКП-001",
        display_name="Гидравлический ковочный пресс №1",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="pre_failure",
        operational_hours=94_000.0,
        maintenance_history_count=11,
        failure_history_count=3,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ГКП-002",
        display_name="Гидравлический ковочный пресс №2",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="normal",
        operational_hours=26_000.0,
        maintenance_history_count=7,
        failure_history_count=1,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ГКП-003",
        display_name="Гидравлический ковочный пресс №3",
        ml_type="Hydraulic_Press",
        category="Прессы",
        state="normal",
        operational_hours=20_000.0,
        maintenance_history_count=5,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ЛШП-001",
        display_name="Листоштамповочный пресс №1",
        ml_type="Press_Brake",
        category="Прессы",
        state="pre_failure",
        operational_hours=98_000.0,
        maintenance_history_count=15,
        failure_history_count=6,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ЛШП-002",
        display_name="Листоштамповочный пресс №2",
        ml_type="Press_Brake",
        category="Прессы",
        state="normal",
        operational_hours=28_000.0,
        maintenance_history_count=7,
        failure_history_count=1,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ЛШП-003",
        display_name="Листоштамповочный пресс №3",
        ml_type="Press_Brake",
        category="Прессы",
        state="normal",
        operational_hours=22_000.0,
        maintenance_history_count=5,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ОП-001",
        display_name="Обрезной пресс №1",
        ml_type="Press_Brake",
        category="Прессы",
        state="normal",
        operational_hours=18_000.0,
        maintenance_history_count=4,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ОП-002",
        display_name="Обрезной пресс №2",
        ml_type="Press_Brake",
        category="Прессы",
        state="pre_failure",
        operational_hours=93_000.0,
        maintenance_history_count=10,
        failure_history_count=3,
        history_days=14,
    ),
    # ------------------------------------------------------------------ #
    # Молоты (8 единиц)
    # ------------------------------------------------------------------ #
    ForgeMachine(
        machine_id="ПВМ-001",
        display_name="Паровоздушный ковочный молот №1",
        ml_type="Boiler",
        category="Молоты",
        state="pre_failure",
        operational_hours=95_500.0,
        maintenance_history_count=13,
        failure_history_count=5,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ПВМ-002",
        display_name="Паровоздушный ковочный молот №2",
        ml_type="Boiler",
        category="Молоты",
        state="normal",
        operational_hours=32_000.0,
        maintenance_history_count=8,
        failure_history_count=1,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ПВМ-003",
        display_name="Паровоздушный ковочный молот №3",
        ml_type="Boiler",
        category="Молоты",
        state="normal",
        operational_hours=24_000.0,
        maintenance_history_count=6,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ПМ-001",
        display_name="Пневматический молот №1",
        ml_type="Compressor",
        category="Молоты",
        state="pre_failure",
        operational_hours=92_500.0,
        maintenance_history_count=10,
        failure_history_count=3,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ПМ-002",
        display_name="Пневматический молот №2",
        ml_type="Compressor",
        category="Молоты",
        state="normal",
        operational_hours=28_000.0,
        maintenance_history_count=7,
        failure_history_count=1,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ФМ-001",
        display_name="Фрикционный молот №1",
        ml_type="Press_Brake",
        category="Молоты",
        state="normal",
        operational_hours=26_000.0,
        maintenance_history_count=6,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ФМ-002",
        display_name="Фрикционный молот №2",
        ml_type="Press_Brake",
        category="Молоты",
        state="normal",
        operational_hours=20_000.0,
        maintenance_history_count=5,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ФМ-003",
        display_name="Фрикционный молот №3",
        ml_type="Press_Brake",
        category="Молоты",
        state="normal",
        operational_hours=30_000.0,
        maintenance_history_count=8,
        failure_history_count=1,
        history_days=14,
    ),
    # ------------------------------------------------------------------ #
    # Нагревательное оборудование (5 единиц)
    # ------------------------------------------------------------------ #
    ForgeMachine(
        machine_id="ИН-001",
        display_name="Индукционный нагреватель №1",
        ml_type="Furnace",
        category="Нагревательное оборудование",
        state="normal",
        operational_hours=28_000.0,
        maintenance_history_count=7,
        failure_history_count=1,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ИН-002",
        display_name="Индукционный нагреватель №2",
        ml_type="Furnace",
        category="Нагревательное оборудование",
        state="normal",
        operational_hours=24_000.0,
        maintenance_history_count=6,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ИН-003",
        display_name="Индукционный нагреватель №3",
        ml_type="Furnace",
        category="Нагревательное оборудование",
        state="new",
        operational_hours=620.0,
        maintenance_history_count=1,
        failure_history_count=0,
        history_days=4,
    ),
    ForgeMachine(
        machine_id="ЭШН-001",
        display_name="Электрошлаковый нагреватель №1",
        ml_type="Furnace",
        category="Нагревательное оборудование",
        state="normal",
        operational_hours=20_000.0,
        maintenance_history_count=5,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ЭШН-002",
        display_name="Электрошлаковый нагреватель №2",
        ml_type="Furnace",
        category="Нагревательное оборудование",
        state="new",
        operational_hours=480.0,
        maintenance_history_count=1,
        failure_history_count=0,
        history_days=3,
    ),
    # ------------------------------------------------------------------ #
    # Вспомогательное оборудование (5 единиц)
    # ------------------------------------------------------------------ #
    ForgeMachine(
        machine_id="КМ-001",
        display_name="Ковочный манипулятор №1",
        ml_type="Robot_Arm",
        category="Вспомогательное оборудование",
        state="normal",
        operational_hours=24_000.0,
        maintenance_history_count=6,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="КМ-002",
        display_name="Ковочный манипулятор №2",
        ml_type="Robot_Arm",
        category="Вспомогательное оборудование",
        state="new",
        operational_hours=310.0,
        maintenance_history_count=1,
        failure_history_count=0,
        history_days=3,
    ),
    ForgeMachine(
        machine_id="ГКМ-001",
        display_name="Горизонтально-ковочная машина №1",
        ml_type="Hydraulic_Press",
        category="Вспомогательное оборудование",
        state="normal",
        operational_hours=22_000.0,
        maintenance_history_count=5,
        failure_history_count=0,
        history_days=14,
    ),
    ForgeMachine(
        machine_id="ГКМ-002",
        display_name="Горизонтально-ковочная машина №2",
        ml_type="Hydraulic_Press",
        category="Вспомогательное оборудование",
        state="new",
        operational_hours=520.0,
        maintenance_history_count=1,
        failure_history_count=0,
        history_days=5,
    ),
    ForgeMachine(
        machine_id="ГБ-001",
        display_name="Галтовочный барабан №1",
        ml_type="Mixer",
        category="Вспомогательное оборудование",
        state="new",
        operational_hours=190.0,
        maintenance_history_count=1,
        failure_history_count=0,
        history_days=2,
    ),
]
