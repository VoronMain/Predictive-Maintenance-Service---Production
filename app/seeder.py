# -*- coding: utf-8 -*-
"""
seeder.py — засев исторических данных кузнечно-прессового цеха.

При первом запуске системы заполняет базу данных 14 днями
ретроспективных измерений телеметрии и предсказаний для 30 агрегатов.
Засев только порождает исторические измерения; всё остальное — окно,
признаки, инференс, предсказания, журнал отказов, часовые агрегаты —
делает конвейер оценки (Pipeline.ingest_history) тем же путём, что и для
живого потока, но без оповещений. Тем самым история и поступающие в
реальном времени данные считаются едиными правилами, что исключает
разрыв прогнозов и степеней критичности на их стыке.

Для предаварийных агрегатов исторический рост риска создаётся разгоном
наработки (Operational_Hours) в пределах окна засева — именно она
служит главным предиктором модели; температура, вибрация и уровни
жидкостей формируют визуальную картину износа, но на модель влияют
слабо.

Засев выполняется один раз — при пустой таблице predictions.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from .db_base import DatabaseProtocol
from .forge_machines import (
    FORGE_MACHINES,
    SEED_HISTORY_DAYS,
    SENSOR_PROFILES,
    ForgeMachine,
    generate_sensor_values,
)
from .pipeline import Pipeline
from .schema import EquipmentRecord, TelemetryMeasurement

log = logging.getLogger(__name__)

# Шаг сетки исторических данных (одно измерение каждые 2 часа).
_STEP_HOURS = 2
# Скорость накопления наработки, ч/ч (агрегат работает ~80 % времени).
_OPS_RATE = 0.8
# Разгон наработки предаварийных агрегатов за окно засева, ч. За 14 дней
# наработка растёт от (текущая − разгон) до текущей, что и формирует
# плавный рост вероятности отказа от ~0.1 до значения, заданного текущей
# наработкой агрегата. Величина демонстрационная, физически наработка так
# быстро не накапливается, но модель получает корректную монотонную картину.
_PRE_FAILURE_RAMP_HOURS = 10_000.0


def is_already_seeded(db: DatabaseProtocol) -> bool:
    """Возвращает True, если в базе данных уже есть предсказания."""
    return db.has_predictions()


def _seed_operational_hours(machine: ForgeMachine, t: float,
                            days_ago: float) -> float:
    """Историческая наработка агрегата для точки t ∈ [0, 1].

    Для предаварийных агрегатов наработка разгоняется от
    (текущая − _PRE_FAILURE_RAMP_HOURS) при t=0 до текущей при t=1, что
    задаёт монотонный рост риска. Для прочих — реалистичное накопление со
    скоростью _OPS_RATE назад во времени от текущего значения.
    """
    if machine.state == "pre_failure":
        return machine.operational_hours - (1.0 - t) * _PRE_FAILURE_RAMP_HOURS
    return max(0.0, machine.operational_hours - days_ago * 24 * _OPS_RATE)


def seed_historical_data(pipeline: Pipeline,
                         machines: list[ForgeMachine] = None,
                         days: int = SEED_HISTORY_DAYS) -> None:
    """Засевает исторические данные для всех агрегатов через конвейер оценки.

    Parameters
    ----------
    pipeline : Pipeline
        Конвейер оценки; история идёт в ingest_history (без оповещений).
    machines : list[ForgeMachine], optional
        Список агрегатов. При None используется FORGE_MACHINES.
    days : int
        Глубина истории в днях (не более history_days конкретного агрегата).
    """
    if machines is None:
        machines = FORGE_MACHINES

    now = datetime.now(timezone.utc)
    measurements: list[TelemetryMeasurement] = []

    for machine in machines:
        effective_days = min(days, machine.history_days)
        n_steps = effective_days * (24 // _STEP_HOURS)
        if n_steps < 1:
            n_steps = 1

        # Регистрация агрегата с категорией.
        pipeline.db.upsert_equipment(EquipmentRecord(
            machine_id=machine.machine_id,
            machine_type=machine.ml_type,
            operational_hours=machine.operational_hours,
            category=machine.category,
        ))

        # Детерминированный генератор шума: уникальный seed на машину.
        rng = random.Random(abs(hash(machine.machine_id)) % (2 ** 32))

        for step in range(n_steps):
            # Временна́я метка: самая ранняя точка → «сейчас».
            dt = now - timedelta(hours=(n_steps - 1 - step) * _STEP_HOURS)
            t = step / max(n_steps - 1, 1)  # нормированное время 0..1
            days_ago = (now - dt).total_seconds() / 86400.0

            sensors = generate_sensor_values(machine, t, rng)

            last_maint = int(
                SENSOR_PROFILES[machine.state]["last_maintenance_days_ago"] - days_ago
            )
            last_maint = max(0, min(365, last_maint))

            ops_hours = _seed_operational_hours(machine, t, days_ago)

            try:
                measurements.append(TelemetryMeasurement(
                    machine_id=machine.machine_id,
                    machine_type=machine.ml_type,
                    timestamp=dt,
                    operational_hours=round(ops_hours, 1),
                    temperature_c=sensors["temperature_c"],
                    vibration_mms=sensors["vibration_mms"],
                    sound_db=sensors["sound_db"],
                    oil_level_pct=sensors["oil_level_pct"],
                    coolant_level_pct=sensors["coolant_level_pct"],
                    power_consumption_kw=sensors["power_consumption_kw"],
                    last_maintenance_days_ago=last_maint,
                    maintenance_history_count=machine.maintenance_history_count,
                    failure_history_count=machine.failure_history_count,
                    ai_supervision=True,
                    error_codes_last_30_days=sensors["error_codes_last_30_days"],
                    ai_override_events=sensors["ai_override_events"],
                ))
            except Exception as exc:
                log.warning("Пропуск некорректного измерения %s t=%.2f: %s",
                            machine.machine_id, t, exc)

    predictions = pipeline.ingest_history(measurements)
    log.info("Засев завершён: %d предсказаний по %d агрегатам",
             len(predictions), len(machines))
