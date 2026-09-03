# -*- coding: utf-8 -*-
"""
seeder.py — засев исторических данных кузнечно-прессового цеха.

При первом запуске системы заполняет базу данных 14 днями
ретроспективных измерений телеметрии и предсказаний для 30 агрегатов.
Предсказания формируются тем же ML-сервисом, что обслуживает живой
поток: по каждому историческому измерению строится вектор признаков
(features.build_feature_vector) и выполняется реальный инференс. Тем
самым история и поступающие в реальном времени данные считаются
единой моделью, что исключает разрыв прогнозов на их стыке.

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
from typing import Optional

import pandas as pd

from .buffer import AggregationManager
from .config import settings
from .db_base import DatabaseProtocol
from .features import build_feature_vector
from .forge_machines import (
    FORGE_MACHINES,
    SEED_HISTORY_DAYS,
    SENSOR_PROFILES,
    ForgeMachine,
    generate_sensor_values,
)
from .ml_service import MLService
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


def seed_historical_data(db: DatabaseProtocol, ml_service: MLService,
                         machines: list[ForgeMachine] = None,
                         days: int = SEED_HISTORY_DAYS) -> None:
    """Засевает исторические данные для всех агрегатов реальным инференсом.

    Parameters
    ----------
    db : SQLiteDatabase | PostgresDatabase
        Экземпляр подсистемы хранения данных.
    ml_service : MLService
        Сервис ML-инференса; те же модели обслуживают живой поток.
    machines : list[ForgeMachine], optional
        Список агрегатов. При None используется FORGE_MACHINES.
    days : int
        Глубина истории в днях (не более history_days конкретного агрегата).
    """
    if machines is None:
        machines = FORGE_MACHINES

    threshold = settings.FAILURE_THRESHOLD
    model = ml_service.model
    now = datetime.now(timezone.utc)
    total_records = 0

    for machine in machines:
        effective_days = min(days, machine.history_days)
        n_steps = effective_days * (24 // _STEP_HOURS)
        if n_steps < 1:
            n_steps = 1

        # Регистрация агрегата с категорией.
        db.upsert_equipment(EquipmentRecord(
            machine_id=machine.machine_id,
            machine_type=machine.ml_type,
            operational_hours=machine.operational_hours,
            category=machine.category,
        ))

        # Детерминированный генератор шума: уникальный seed на машину.
        rng = random.Random(abs(hash(machine.machine_id)) % (2 ** 32))

        # Буфер агрегации повторяет путь живого конвейера: окно меньше шага
        # сетки (2 ч), поэтому в каждый момент агрегат строится по одному
        # последнему измерению — как при потоковой обработке.
        aggregator = AggregationManager(
            window_seconds=settings.AGGREGATION_WINDOW_SECONDS
        )

        incident_id: Optional[int] = None
        telemetry_rows: list[tuple] = []
        # Параллельные списки: метки времени и векторы признаков для пакетного
        # инференса по всему окну агрегата за один вызов модели.
        timestamps: list[datetime] = []
        feature_frames: list[pd.DataFrame] = []

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
                measurement = TelemetryMeasurement(
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
                )
            except Exception as exc:
                log.warning("Пропуск некорректного измерения %s t=%.2f: %s",
                            machine.machine_id, t, exc)
                continue

            payload = measurement.model_dump(mode="json")
            ts_iso = dt.isoformat()
            telemetry_rows.append((
                machine.machine_id,
                ts_iso,
                __import__("json").dumps(payload, ensure_ascii=False),
            ))

            aggregator.append(measurement)
            aggregate = aggregator.aggregate(machine.machine_id)
            feature_frames.append(build_feature_vector(aggregate))
            timestamps.append(dt)

        if not feature_frames:
            continue

        # Пакетный инференс по всему окну агрегата: один вызов на каждую модель.
        frame = pd.concat(feature_frames, ignore_index=True)
        failure = model.predict_failure(frame, threshold=threshold)
        rul = model.predict_rul(frame)

        prediction_rows: list[tuple] = []
        incident_events: list[tuple] = []
        for i, dt in enumerate(timestamps):
            prob = float(failure.probability[i])
            label = int(failure.label[i])
            rul_days = float(rul.rul_days[i])
            prediction_rows.append((
                machine.machine_id, dt.isoformat(),
                round(prob, 4), label, round(rul_days, 2), threshold,
            ))
            incident_events.append((dt, prob, rul_days))
        total_records += len(prediction_rows)

        # Пакетная вставка всех строк одной транзакцией.
        db.bulk_insert_for_seed(telemetry_rows, prediction_rows)

        # Открываем/обновляем инцидент для предаварийных агрегатов.
        for dt, prob, rul_days in incident_events:
            if machine.state != "pre_failure":
                break
            if incident_id is None and prob >= threshold:
                severity = ("critical" if prob >= 0.55
                            else "high" if prob >= 0.45 else "medium")
                try:
                    incident_id = db.open_incident(
                        machine.machine_id, dt, prob, rul_days, threshold, severity
                    )
                except Exception as exc:
                    log.warning("Ошибка открытия инцидента %s: %s",
                                machine.machine_id, exc)
            elif incident_id is not None and prob > threshold:
                peak_sev = "critical" if prob >= 0.55 else "high"
                try:
                    db.update_open_incident(incident_id, prob, rul_days, peak_sev)
                except Exception:
                    pass

        log.debug("Засев %s: %d точек (состояние=%s)",
                  machine.machine_id, n_steps, machine.state)

    log.info("Засев завершён: %d записей по %d агрегатам",
             total_records, len(machines))
