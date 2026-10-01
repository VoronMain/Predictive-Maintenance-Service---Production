# -*- coding: utf-8 -*-
"""
trajectory.py — траектория агрегата: единственное место, где собирается
измерение телеметрии демо-стенда.

По агрегату, прогрессу деградации ``t`` и моменту времени ``measure``
выдаёт полное измерение (все датчики, наработку, дни с последнего ТО,
счётчики и метаданные) в типе, который принимает конвейер и валидирует
``/ingest``. Засев вызывает его для точек истории t ∈ [0, 1], эмулятор —
для живого потока t ≥ 1; поэтому смещения агрегата, тренды и правила
наработки на стыке совпадают по построению.

Всё случайное выводится из идентификатора агрегата стабильной хеш-функцией
(SHA-256), а не встроенным ``hash()``: тот рандомизируется в каждом
процессе, а засев (внутри HTTP-сервиса) и эмулятор — разные процессы.

Правила
-------
* Прогресс t нормирован на окно засева агрегата: t = 0 — начало истории,
  t = 1 — «сейчас» (конец истории, старт живого потока), t > 1 — поток.
* Наработка. В истории: у предаварийных агрегатов разгон от
  (справочная − _PRE_FAILURE_RAMP_HOURS) до справочной, у прочих —
  накопление _OPS_RATE ч/ч назад от справочной. При t > 1 наработка не
  наращивается: справочное значение плюс шум до 1 ч.
* Дни с последнего ТО. В истории — от профиля назад во времени, при
  t ≥ 1 — профиль с небольшим шумом.
* Датчики предаварийных агрегатов продолжают тренд деградации при t > 1;
  у normal/new значения стационарны вокруг профиля.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime

from .forge_machines import (
    SEED_HISTORY_DAYS,
    SENSOR_PROFILES,
    ForgeMachine,
)
from .schema import TelemetryMeasurement

# Скорость накопления наработки, ч/ч (агрегат работает ~80 % времени).
_OPS_RATE = 0.8
# Разгон наработки предаварийных агрегатов за окно засева, ч. За 14 дней
# наработка растёт от (текущая − разгон) до текущей, что и формирует
# плавный рост вероятности отказа от ~0.1 до значения, заданного текущей
# наработкой агрегата. Величина демонстрационная, физически наработка так
# быстро не накапливается, но модель получает корректную монотонную картину.
_PRE_FAILURE_RAMP_HOURS = 10_000.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _digest(machine_id: str) -> bytes:
    return hashlib.sha256(machine_id.encode("utf-8")).digest()


def _offset(machine: ForgeMachine) -> float:
    """Постоянное смещение агрегата в диапазоне [0, 1) от масштаба профиля."""
    return int.from_bytes(_digest(machine.machine_id)[4:8], "big") % 1000 / 1000.0


def noise_source(machine: ForgeMachine) -> random.Random:
    """Источник шума агрегата с начальным состоянием, стабильным между
    процессами и запусками."""
    return random.Random(int.from_bytes(_digest(machine.machine_id)[:4], "big"))


def _window_days(machine: ForgeMachine) -> int:
    return max(min(SEED_HISTORY_DAYS, machine.history_days), 1)


def _sensors(machine: ForgeMachine, t: float, rng: random.Random) -> dict:
    """Значения 6 датчиков + коды ошибок / override для прогресса t.

    Для предаварийных агрегатов накладывает монотонный тренд деградации:
    при t ∈ [0, 1] он совпадает с окном засева, а при t > 1 продолжается
    за его пределы. Для normal/new агрегатов t игнорируется — значения
    стационарны вокруг базового профиля.
    """
    profile = SENSOR_PROFILES[machine.state]
    m_seed = _offset(machine)

    if machine.state == "pre_failure":
        # Деградация: температура и вибрация растут, масло и ОЖ снижаются.
        # Эти признаки формируют визуальную картину износа на дашборде;
        # на саму ML-модель они влияют слабо (риск задаёт наработка).
        temp = profile["temperature_c"] + t * 6.0 + m_seed * 4.0
        vib = profile["vibration_mms"] + t * 5.0 + m_seed * 2.0
        oil = profile["oil_level_pct"] - t * 7.0 - m_seed * 3.0
        cool = profile["coolant_level_pct"] - t * 5.0 - m_seed * 2.0
        sound = profile["sound_db"] + t * 3.0 + m_seed * 2.0
        power = profile["power_consumption_kw"] + t * 30.0 + m_seed * 20.0
        # Коды ошибок и AI-override НЕ наращиваем по времени: см. примечание
        # к профилю pre_failure — их рост снижал бы расчётную вероятность.
        err = int(profile["error_codes_last_30_days"])
        overr = int(profile["ai_override_events"])
    else:
        temp = profile["temperature_c"] + m_seed * 8.0
        vib = profile["vibration_mms"] + m_seed * 4.0
        oil = profile["oil_level_pct"] - m_seed * 10.0
        cool = profile["coolant_level_pct"] - m_seed * 8.0
        sound = profile["sound_db"] + m_seed * 4.0
        power = profile["power_consumption_kw"] + m_seed * 50.0
        err = profile["error_codes_last_30_days"]
        overr = profile["ai_override_events"]

    # Нормальный шум — имитирует естественные флуктуации.
    temp += rng.gauss(0, 1.5)
    vib += rng.gauss(0, 0.5)
    oil += rng.gauss(0, 1.0)
    cool += rng.gauss(0, 1.0)
    sound += rng.gauss(0, 1.2)
    power += rng.gauss(0, 8.0)

    return {
        "temperature_c": round(_clamp(temp, -50, 200), 1),
        "vibration_mms": round(_clamp(vib, 0, 50), 2),
        "sound_db": round(_clamp(sound, 0, 140), 1),
        "oil_level_pct": round(_clamp(oil, 0, 100), 1),
        "coolant_level_pct": round(_clamp(cool, 0, 100), 1),
        "power_consumption_kw": round(_clamp(power, 0, 600), 1),
        "error_codes_last_30_days": int(_clamp(err + rng.randint(-1, 1), 0, 100)),
        "ai_override_events": int(_clamp(overr, 0, 50)),
    }


def _operational_hours(machine: ForgeMachine, t: float,
                       rng: random.Random) -> float:
    if t > 1.0:
        # Живой поток: наработка не наращивается (вероятность отказа на
        # стенде задаётся ею и не должна уплывать за дни работы).
        return machine.operational_hours + rng.uniform(0, 1)
    if machine.state == "pre_failure":
        return machine.operational_hours - (1.0 - t) * _PRE_FAILURE_RAMP_HOURS
    days_ago = (1.0 - t) * _window_days(machine)
    return max(0.0, machine.operational_hours - days_ago * 24 * _OPS_RATE)


def _last_maintenance_days_ago(machine: ForgeMachine, t: float,
                               rng: random.Random) -> int:
    base = SENSOR_PROFILES[machine.state]["last_maintenance_days_ago"]
    if t >= 1.0:
        spread = 2 if machine.state == "pre_failure" else 1
        days = base + rng.randint(-spread, spread)
    else:
        days = int(base - (1.0 - t) * _window_days(machine))
    return int(_clamp(days, 0, 365))


def measure(machine: ForgeMachine, t: float, at: datetime,
            rng: random.Random) -> TelemetryMeasurement:
    """Полное измерение телеметрии агрегата для прогресса деградации t.

    ``at`` — момент измерения (попадает в timestamp), ``rng`` — источник
    шума, обычно ``noise_source(machine)``; он расходуется по ходу вызовов.
    Невалидное измерение не пропускается молча: схема телеметрии
    выбрасывает ValidationError.
    """
    sensors = _sensors(machine, t, rng)
    ops_hours = _operational_hours(machine, t, rng)
    last_maint = _last_maintenance_days_ago(machine, t, rng)

    return TelemetryMeasurement(
        machine_id=machine.machine_id,
        machine_type=machine.ml_type,
        timestamp=at,
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
        laser_intensity=None,
        hydraulic_pressure_bar=None,
        coolant_flow_l_min=None,
        heat_index=None,
    )
