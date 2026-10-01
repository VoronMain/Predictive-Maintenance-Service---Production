# -*- coding: utf-8 -*-
"""
inference_test.py — тест калиброванной вероятности отказа (issue #11).

Изотонический калибратор с out_of_bounds='clip' отображает весь левый
хвост сырых оценок LightGBM в ровный 0.0, из-за чего живой инференс на
стенде отдавал ровно p=0.000 у здорового оборудования. Проверяем, что
PredictiveMaintenanceModel.predict_failure_proba поджимает результат в
[PROBABILITY_EPSILON, 1 - PROBABILITY_EPSILON] и абсолютных нулей/единиц
больше не выдаёт, сохраняя при этом монотонность и метку по порогу.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.feature_window import FeatureWindow  # noqa: E402
from app.schema import TelemetryMeasurement  # noqa: E402
from predictive_maintenance import PredictiveMaintenanceModel  # noqa: E402
from predictive_maintenance.inference import PROBABILITY_EPSILON  # noqa: E402

_MODELS_DIR = _ROOT / "predictive_maintenance" / "models"


def test_repo_package_not_shadowed_by_external_models_dir():
    """Пакет должен грузиться из дерева репозитория, а не из ..\\Модели
    (см. app.utils._ensure_models_on_path — каталог добавляется в конец
    sys.path). Иначе фикс issue #11 не применялся бы локально."""
    import predictive_maintenance

    loaded_from = Path(predictive_maintenance.__file__).resolve()
    assert _ROOT in loaded_from.parents


@pytest.fixture(scope="module")
def model() -> PredictiveMaintenanceModel:
    return PredictiveMaintenanceModel.load(_MODELS_DIR)


def _features(machine_type: str, operational_hours: float):
    """Вектор признаков из измерения — теми же путём, что и в конвейере."""
    measurement = TelemetryMeasurement(
        machine_id="T-1", machine_type=machine_type,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        operational_hours=operational_hours,
        temperature_c=60.0, vibration_mms=11.0, sound_db=93.0,
        oil_level_pct=65.0, coolant_level_pct=70.0,
        power_consumption_kw=270.0, last_maintenance_days_ago=30.0,
        maintenance_history_count=6, failure_history_count=0,
        ai_supervision=True, error_codes_last_30_days=2.0,
        ai_override_events=1.0,
    )
    return FeatureWindow(window_seconds=3600).add(measurement).features


def _proba(model: PredictiveMaintenanceModel, **kw) -> float:
    return float(model.predict_failure_proba(_features(**kw))[0])


def test_healthy_equipment_probability_is_nonzero(model):
    """Здоровый агрегат: раньше ровно 0.0, теперь — маленькое ненулевое."""
    p = _proba(model, machine_type="Furnace", operational_hours=24_000.0)
    assert p >= PROBABILITY_EPSILON      # раньше здесь был ровный 0.0
    assert p < 0.001  # ниже разрешения отображения — на UI это "<0.001"


@pytest.mark.parametrize(
    "machine_type,ops_hours",
    [
        ("Furnace", 200.0),
        ("Hydraulic_Press", 20_000.0),
        ("Press_Brake", 30_000.0),
        ("Boiler", 32_000.0),
        ("Robot_Arm", 24_000.0),
        ("Compressor", 28_000.0),
        ("Hydraulic_Press", 96_000.0),
        ("Press_Brake", 98_000.0),
    ],
)
def test_probability_strictly_inside_unit_interval(model, machine_type, ops_hours):
    p = _proba(model, machine_type=machine_type, operational_hours=ops_hours)
    assert PROBABILITY_EPSILON <= p <= 1.0 - PROBABILITY_EPSILON


def test_probability_is_monotonic_in_operational_hours(model):
    """Наработка — главный предиктор: больше часов → не ниже вероятность."""
    low = _proba(model, machine_type="Hydraulic_Press", operational_hours=20_000.0)
    mid = _proba(model, machine_type="Hydraulic_Press", operational_hours=90_000.0)
    high = _proba(model, machine_type="Hydraulic_Press", operational_hours=98_000.0)
    assert low <= mid <= high
    assert high > model.threshold  # предаварийный агрегат всё ещё ловится порогом


def test_clip_does_not_move_label_across_threshold(model):
    """Поджатие на 1e-4 не должно менять бинарную метку по порогу t*."""
    frame = _features(machine_type="Furnace", operational_hours=24_000.0)
    failure = model.predict_failure(frame)
    assert failure.label[0] == 0
    assert 0.0 < failure.probability[0] < model.threshold
