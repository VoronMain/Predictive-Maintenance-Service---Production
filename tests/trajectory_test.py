# -*- coding: utf-8 -*-
"""
trajectory_test.py — поведенческие тесты module «траектория агрегата».

Единственный seam — interface app.trajectory: measure() и noise_source().
Проверяются свойства выданного измерения (стабильность, воспроизводимость,
непрерывность на стыке, валидность, правило t > 1), а не то, как
вычисляется смещение или какая хеш-функция лежит под seed.
Docker и сеть не нужны.
"""
from __future__ import annotations

import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.forge_machines import FORGE_MACHINES
from app.schema import TelemetryMeasurement
from app.trajectory import measure, noise_source

_ROOT = Path(__file__).resolve().parent.parent
_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

_PRE_FAILURE = [m for m in FORGE_MACHINES if m.state == "pre_failure"]
_NORMAL = [m for m in FORGE_MACHINES if m.state == "normal"]


def _mean(machine, t: float, field: str, n: int = 60) -> float:
    """Среднее поля по n измерениям с независимым шумом."""
    rng = noise_source(machine)
    return statistics.fmean(
        getattr(measure(machine, t, _AT, rng), field) for _ in range(n))


# --------------------------------------------------------------- #
# Стабильность и воспроизводимость
# --------------------------------------------------------------- #

_CHILD = (
    "from datetime import datetime, timezone;"
    "from app.forge_machines import FORGE_MACHINES;"
    "from app.trajectory import measure, noise_source;"
    "m = FORGE_MACHINES[{index}];"
    "at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc);"
    "print(measure(m, {t}, at, noise_source(m)).model_dump_json())"
)


def _measurement_in_child(hash_seed: str, index: int, t: float) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed,
           "PYTHONPATH": str(_ROOT)}
    out = subprocess.run(
        [sys.executable, "-c", _CHILD.format(index=index, t=t)],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=True,
        timeout=60)
    return out.stdout.strip()


@pytest.mark.parametrize("index", [0, 2, 20])
@pytest.mark.parametrize("t", [0.3, 1.0, 1.7])
def test_measurement_identical_across_processes(index, t):
    """Разные PYTHONHASHSEED — точно то же измерение."""
    first = _measurement_in_child("1", index, t)
    second = _measurement_in_child("12345", index, t)
    assert first and first == second


def test_same_inputs_same_measurement_in_one_process():
    machine = FORGE_MACHINES[0]
    a = measure(machine, 0.5, _AT, noise_source(machine))
    b = measure(machine, 0.5, _AT, noise_source(machine))
    assert a == b


# --------------------------------------------------------------- #
# Стык истории и потока
# --------------------------------------------------------------- #

@pytest.mark.parametrize("machine", FORGE_MACHINES,
                         ids=lambda m: m.machine_id)
def test_history_end_and_stream_start_coincide(machine):
    """Последняя точка истории и первая точка потока (t = 1) — одно измерение."""
    last_history = measure(machine, 1.0, _AT, noise_source(machine))
    first_stream = measure(machine, 1.0, _AT, noise_source(machine))
    assert last_history == first_stream


@pytest.mark.parametrize("machine", FORGE_MACHINES,
                         ids=lambda m: m.machine_id)
def test_no_jump_in_maintenance_days_across_junction(machine):
    """Дни с ТО у конца истории и у начала потока различаются на единицы,
    а не на десятки дней."""
    rng = noise_source(machine)
    before = measure(machine, 0.99, _AT, rng).last_maintenance_days_ago
    after = measure(machine, 1.01, _AT, rng).last_maintenance_days_ago
    assert abs(before - after) <= 4


# --------------------------------------------------------------- #
# Валидность
# --------------------------------------------------------------- #

def test_every_measurement_passes_telemetry_schema():
    grid = [i / 20 for i in range(41)]  # t ∈ [0, 2]
    for machine in FORGE_MACHINES:
        rng = noise_source(machine)
        for t in grid:
            m = measure(machine, t, _AT, rng)
            assert isinstance(m, TelemetryMeasurement)
            TelemetryMeasurement.model_validate(m.model_dump())
            assert m.machine_id == machine.machine_id
            assert m.machine_type == machine.ml_type
            assert m.timestamp == _AT


# --------------------------------------------------------------- #
# Правила наработки и датчиков
# --------------------------------------------------------------- #

@pytest.mark.parametrize("machine", FORGE_MACHINES,
                         ids=lambda m: m.machine_id)
def test_stream_does_not_accumulate_operational_hours(machine):
    """При t > 1 наработка — значение из справочника с шумом < 1 ч."""
    rng = noise_source(machine)
    for t in (1.2, 1.6, 2.0):
        ops = measure(machine, t, _AT, rng).operational_hours
        assert machine.operational_hours <= ops <= machine.operational_hours + 1.0


@pytest.mark.parametrize("machine", _PRE_FAILURE,
                         ids=lambda m: m.machine_id)
def test_history_operational_hours_ramp_for_pre_failure(machine):
    start = measure(machine, 0.0, _AT, noise_source(machine)).operational_hours
    end = measure(machine, 1.0, _AT, noise_source(machine)).operational_hours
    assert end == pytest.approx(machine.operational_hours, abs=1.0)
    assert start < end - 5_000


def test_history_operational_hours_accumulate_for_normal_machine():
    machine = _NORMAL[0]
    start = measure(machine, 0.0, _AT, noise_source(machine)).operational_hours
    end = measure(machine, 1.0, _AT, noise_source(machine)).operational_hours
    assert 0 < end - start < 400  # ~0.8 ч/ч за 14 суток


@pytest.mark.parametrize("machine", _PRE_FAILURE,
                         ids=lambda m: m.machine_id)
def test_pre_failure_sensors_keep_degrading_after_t1(machine):
    assert _mean(machine, 2.0, "temperature_c") > _mean(machine, 1.0, "temperature_c") + 3
    assert _mean(machine, 2.0, "vibration_mms") > _mean(machine, 1.0, "vibration_mms") + 2
    assert _mean(machine, 2.0, "oil_level_pct") < _mean(machine, 1.0, "oil_level_pct") - 3


def test_normal_machine_sensors_are_stationary():
    machine = _NORMAL[0]
    assert (_mean(machine, 0.0, "temperature_c")
            == pytest.approx(_mean(machine, 2.0, "temperature_c"), abs=1.5))


# --------------------------------------------------------------- #
# Различимость агрегатов
# --------------------------------------------------------------- #

def test_machines_of_same_state_have_different_offsets():
    means = [_mean(m, 1.0, "temperature_c", n=200) for m in _NORMAL]
    assert max(means) - min(means) > 1.0
    # ни одной пары практически идентичных агрегатов
    ordered = sorted(means)
    assert len({round(x, 1) for x in ordered}) > len(ordered) // 2
