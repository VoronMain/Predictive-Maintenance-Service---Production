# -*- coding: utf-8 -*-
"""
seeder.py — засев исторических данных кузнечно-прессового цеха.

При первом запуске системы заполняет базу данных 14 днями
ретроспективных измерений телеметрии и предсказаний для 30 агрегатов.
Засев только расставляет точки сетки и берёт измерение для каждой у
module траектории агрегата (app/trajectory.py); всё остальное — окно,
признаки, инференс, предсказания, журнал отказов, часовые агрегаты —
делает конвейер оценки (Pipeline.ingest_history) тем же путём, что и для
живого потока, но без оповещений. Тем самым история и поступающие в
реальном времени данные считаются едиными правилами, что исключает
разрыв прогнозов и степеней критичности на их стыке.

Засев выполняется один раз — при пустой таблице predictions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .db_base import DatabaseProtocol
from .forge_machines import FORGE_MACHINES, SEED_HISTORY_DAYS, ForgeMachine
from .pipeline import Pipeline
from .schema import EquipmentRecord, TelemetryMeasurement
from .trajectory import measure, noise_source

log = logging.getLogger(__name__)

# Шаг сетки исторических данных (одно измерение каждые 2 часа).
_STEP_HOURS = 2


def is_already_seeded(db: DatabaseProtocol) -> bool:
    """Возвращает True, если в базе данных уже есть предсказания."""
    return db.has_predictions()


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

        rng = noise_source(machine)

        for step in range(n_steps):
            # Временна́я метка: самая ранняя точка → «сейчас».
            dt = now - timedelta(hours=(n_steps - 1 - step) * _STEP_HOURS)
            t = step / max(n_steps - 1, 1)  # нормированное время 0..1
            measurements.append(measure(machine, t, dt, rng))

    predictions = pipeline.ingest_history(measurements)
    log.info("Засев завершён: %d предсказаний по %d агрегатам",
             len(predictions), len(machines))
