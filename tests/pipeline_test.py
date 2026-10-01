# -*- coding: utf-8 -*-
"""
pipeline_test.py — тест единого пути оценки (issue #19).

Единственный seam — interface конвейера оценки (app.pipeline.Pipeline):
измерения подаются поштучно (ingest, живой поток) и пачкой
(ingest_history, засев), а результат проверяется через чтения
хранилища: предсказания, журнал отказов, журнал оповещений. Окно,
признаки и вызовы модели напрямую не тестируются.

Окружение без моков: настоящий адаптер SQLite во временном каталоге и
настоящие артефакты моделей из репозитория. Вероятность отказа
управляется наработкой (operational_hours) — главным предиктором
модели: при прочих равных ~70 тыс. ч — норма, ~94 тыс. ч — p≈0.42
(умеренная), ~96 тыс. ч — p≈0.61 (высокая), ~100 тыс. ч — p≈0.82
(критическая). Шаг между измерениями 2 ч больше окна агрегации, поэтому
признаки каждого измерения определяются им самим.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import settings  # noqa: E402
from app.database import SQLiteDatabase  # noqa: E402
from app.feature_window import FeatureWindow  # noqa: E402
from app.forge_machines import FORGE_MACHINES  # noqa: E402
from app.incidents import IncidentDetector  # noqa: E402
from app.ml_service import MLService  # noqa: E402
from app.notifications import FileEmailTransport, NotificationService  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.schema import EquipmentRecord, TelemetryMeasurement  # noqa: E402
from app.seeder import is_already_seeded, seed_historical_data  # noqa: E402

_MODELS_DIR = _ROOT / "predictive_maintenance" / "models"
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_STEP = timedelta(hours=2)

# Наработка → уровень риска (см. docstring модуля).
NORMAL, MEDIUM, HIGH, CRITICAL = 70_000.0, 94_000.0, 96_000.0, 100_000.0


@pytest.fixture(scope="module")
def ml() -> MLService:
    return MLService.from_path(_MODELS_DIR, threshold=settings.FAILURE_THRESHOLD)


class Stand:
    """Конвейер на чистом SQLite плюс подсистема оповещений (file-режим)."""

    def __init__(self, ml: MLService, tmp: Path, min_samples: int = 1) -> None:
        self.db = SQLiteDatabase(tmp / "spa.db")
        self.notifier = NotificationService(
            db=self.db, transport=FileEmailTransport(tmp / "alerts"),
            sender="spa@example.local", recipient="ops@example.local", mode="file",
        )
        self.pipeline = Pipeline(
            db=self.db,
            window=FeatureWindow(settings.AGGREGATION_WINDOW_SECONDS, min_samples),
            ml_service=ml,
            incident_detector=IncidentDetector(db=self.db),
            notifier=self.notifier,
        )
        self.registered: set[str] = set()

    def register(self, *measurements: TelemetryMeasurement) -> None:
        """Справочник агрегатов ведёт засев, а не конвейер истории."""
        for m in measurements:
            if m.machine_id not in self.registered:
                self.db.upsert_equipment(EquipmentRecord(
                    machine_id=m.machine_id, machine_type=m.machine_type,
                    operational_hours=m.operational_hours,
                ))
                self.registered.add(m.machine_id)

    def incidents(self) -> list[dict]:
        rows = self.db.list_incidents(limit=1000)
        return sorted(rows, key=lambda r: (r["machine_id"], r["opened_at"]))

    def predictions(self, machine_id: str) -> list[dict]:
        rows = self.db.predictions_history(machine_id, limit=1000)
        return sorted(rows, key=lambda r: r["timestamp"])

    def hourly(self, machine_id: str) -> list[dict]:
        """Сохранённые векторы признаков (часовые агрегаты), по времени."""
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT features FROM telemetry_hourly WHERE machine_id = ? "
                "ORDER BY window_end", (machine_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def alerts(self) -> list[dict]:
        self.notifier.flush_all()
        return self.db.list_alerts(limit=1000)


@pytest.fixture()
def stand(ml, tmp_path) -> Stand:
    return Stand(ml, tmp_path)


@pytest.fixture()
def second_stand(ml, tmp_path) -> Stand:
    other = tmp_path / "second"
    other.mkdir()
    return Stand(ml, other)


def _measurement(machine_id: str, hours: float, step: int) -> TelemetryMeasurement:
    return TelemetryMeasurement(
        machine_id=machine_id, machine_type="Hydraulic_Press",
        timestamp=_T0 + step * _STEP, operational_hours=hours,
        temperature_c=60, vibration_mms=8, sound_db=85, oil_level_pct=60,
        coolant_level_pct=70, power_consumption_kw=200,
        last_maintenance_days_ago=30, maintenance_history_count=3,
        failure_history_count=1, ai_supervision=True,
        error_codes_last_30_days=2, ai_override_events=0,
    )


def _series(machine_id: str, levels: list[float]) -> list[TelemetryMeasurement]:
    return [_measurement(machine_id, h, i) for i, h in enumerate(levels)]


def _feed_one_by_one(stand: Stand, measurements) -> None:
    for m in sorted(measurements, key=lambda x: x.timestamp):
        stand.pipeline.ingest(m)


# ---------------------------------------------------------------- критичность

def test_severity_follows_probability_thresholds_from_config(stand):
    ms = _series("P-1", [MEDIUM]) + _series("P-2", [HIGH]) + _series("P-3", [CRITICAL])
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)

    by_machine = {i["machine_id"]: i["severity"] for i in stand.incidents()}
    assert by_machine == {"P-1": "medium", "P-2": "high", "P-3": "critical"}


def test_changed_probability_thresholds_apply_to_history(stand, monkeypatch):
    monkeypatch.setattr(settings, "SEVERITY_HIGH_PROB", 0.40)
    monkeypatch.setattr(settings, "SEVERITY_CRITICAL_PROB", 0.55)
    ms = _series("P-1", [MEDIUM]) + _series("P-2", [HIGH])
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)

    by_machine = {i["machine_id"]: i["severity"] for i in stand.incidents()}
    # 0.42 ≥ 0.40 → high; 0.61 ≥ 0.55 → critical.
    assert by_machine == {"P-1": "high", "P-2": "critical"}


def test_low_remaining_life_raises_severity_in_history(stand, monkeypatch):
    # При умеренной вероятности (p≈0.42, RUL≈28 дн.) правило RUL ≤ 14 → high
    # и RUL ≤ 7 → critical срабатывает, когда пороги RUL подняты над ним.
    monkeypatch.setattr(settings, "SEVERITY_HIGH_RUL_DAYS", 30.0)
    high = _series("R-1", [MEDIUM])
    stand.register(*high)
    stand.pipeline.ingest_history(high)
    assert stand.incidents()[0]["severity"] == "high"

    monkeypatch.setattr(settings, "SEVERITY_CRITICAL_RUL_DAYS", 40.0)
    critical = _series("R-2", [MEDIUM])
    stand.register(*critical)
    stand.pipeline.ingest_history(critical)
    by_machine = {i["machine_id"]: i["severity"] for i in stand.incidents()}
    assert by_machine["R-2"] == "critical"


# --------------------------------------------------------- жизненный цикл

def test_incident_lifecycle_open_update_close_reopen(stand):
    levels = [NORMAL, MEDIUM, HIGH, CRITICAL, NORMAL, NORMAL, HIGH]
    ms = _series("L-1", levels)
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)

    first, second = stand.incidents()
    # Открыт при переходе через t*, обновлялся, пик — максимум за время
    # инцидента, закрыт при возврате в норму.
    assert first["opened_at"] == ms[1].timestamp.isoformat()
    assert first["severity"] == "medium"
    assert first["peak_severity"] == "critical"
    assert first["status"] == "closed"
    assert first["closed_at"] == ms[4].timestamp.isoformat()
    # Повторный переход через t* — новый инцидент, ещё открытый.
    assert second["opened_at"] == ms[6].timestamp.isoformat()
    assert second["status"] == "open"
    assert second["peak_severity"] == "high"


def test_peak_severity_is_actual_maximum_not_forced_high(stand):
    ms = _series("L-2", [MEDIUM, MEDIUM, MEDIUM])
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)

    (incident,) = stand.incidents()
    assert incident["peak_severity"] == "medium"


# -------------------------------------------------------------- эквивалентность

def test_batch_history_equals_one_by_one_ingest(stand, second_stand):
    batch = (
        _series("E-1", [NORMAL, MEDIUM, HIGH, CRITICAL, NORMAL, HIGH])
        + _series("E-2", [NORMAL, NORMAL, MEDIUM, MEDIUM, CRITICAL, CRITICAL])
        + _series("E-3", [NORMAL] * 6)
    )
    stand.register(*batch)
    stand.pipeline.ingest_history(batch)
    _feed_one_by_one(second_stand, batch)

    for machine_id in ("E-1", "E-2", "E-3"):
        got, want = stand.predictions(machine_id), second_stand.predictions(machine_id)
        assert len(got) == len(want) == 6
        for g, w in zip(got, want):
            assert g["timestamp"] == w["timestamp"]
            assert g["failure_label"] == w["failure_label"]
            assert g["failure_probability"] == pytest.approx(
                w["failure_probability"], abs=1e-9)
            assert g["remaining_useful_life_days"] == pytest.approx(
                w["remaining_useful_life_days"], abs=1e-6)

    def journal(s: Stand):
        return [{k: i[k] for k in ("machine_id", "opened_at", "closed_at", "severity",
                                   "peak_severity", "status")}
                for i in s.incidents()]

    assert journal(stand) == journal(second_stand)
    assert len(journal(stand)) == 3  # E-1 дважды, E-2 один раз


def test_history_keeps_full_precision_of_probability_and_rul(stand, second_stand):
    ms = _series("Q-1", [MEDIUM])
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)
    _feed_one_by_one(second_stand, ms)

    got = stand.predictions("Q-1")[0]
    want = second_stand.predictions("Q-1")[0]
    assert got["failure_probability"] == want["failure_probability"]
    assert got["remaining_useful_life_days"] == want["remaining_useful_life_days"]


def test_history_stores_hourly_aggregates_like_live_path(stand, second_stand):
    ms = _series("H-1", [NORMAL, MEDIUM, HIGH])
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)
    _feed_one_by_one(second_stand, ms)

    def hourly_count(s: Stand) -> int:
        with s.db._lock:
            return s.db._conn.execute(
                "SELECT COUNT(*) FROM telemetry_hourly").fetchone()[0]

    assert hourly_count(stand) == hourly_count(second_stand) == 3


# -------------------------------------------------------------- оповещения

def test_history_never_alerts_but_live_ingest_does(stand):
    history = _series("A-1", [NORMAL, CRITICAL, CRITICAL])
    stand.register(*history)
    stand.pipeline.ingest_history(history)
    assert stand.incidents()[0]["status"] == "open"
    assert stand.alerts() == []

    live = _series("A-2", [CRITICAL])
    stand.register(*live)
    stand.pipeline.ingest(live[0])
    assert len(stand.alerts()) >= 1


# ---------------------------------------------------------------------- стык

def test_incident_open_at_end_of_history_continues_live(stand):
    history = _series("J-1", [NORMAL, MEDIUM, HIGH])
    stand.register(*history)
    stand.pipeline.ingest_history(history)
    (opened,) = stand.incidents()
    assert opened["status"] == "open"

    live = _measurement("J-1", CRITICAL, step=3)
    stand.pipeline.ingest(live)

    (same,) = stand.incidents()  # новый не появился
    assert same["id"] == opened["id"]
    assert same["peak_severity"] == "critical"


# ------------------------------------------------------------ изоляция ошибок

def test_failure_of_one_machine_is_logged_and_does_not_stop_others(stand, caplog):
    good = _series("G-1", [NORMAL, MEDIUM, HIGH])
    bad = _series("G-BAD", [NORMAL, MEDIUM])  # не в справочнике → сбой записи
    stand.register(*good)

    with caplog.at_level("ERROR"):
        predictions = stand.pipeline.ingest_history(good + bad)

    assert {p.machine_id for p in predictions} == {"G-1"}
    assert len(stand.predictions("G-1")) == 3
    assert stand.predictions("G-BAD") == []
    assert [i["machine_id"] for i in stand.incidents()] == ["G-1"]
    failures = [r for r in caplog.records if "G-BAD" in r.getMessage()]
    assert failures and failures[0].exc_info  # с трейсбеком, а не молча


# --------------------------------------------------------------------- засев

@pytest.fixture(scope="module")
def seeded(ml, tmp_path_factory):
    stand = Stand(ml, tmp_path_factory.mktemp("seed"))
    seed_historical_data(stand.pipeline, FORGE_MACHINES)
    return stand


def test_seed_fills_every_machine_and_skips_on_restart(seeded):
    assert is_already_seeded(seeded.db)
    for machine in FORGE_MACHINES:
        assert seeded.predictions(machine.machine_id), machine.machine_id
    equipment = {e["machine_id"]: e for e in seeded.db.list_equipment()}
    assert set(equipment) == {m.machine_id for m in FORGE_MACHINES}
    assert all(e["category"] for e in equipment.values())


def test_seed_sends_no_alerts(seeded):
    assert seeded.db.get_alerts_count() == 0
    assert seeded.alerts() == []


def test_seeded_incidents_use_same_severity_rules_as_live(seeded):
    rows = seeded.incidents()
    assert rows, "у предаварийных агрегатов должны быть инциденты"
    for incident in rows:
        assert incident["severity"] in {"medium", "high", "critical"}
        # Пик никогда не ниже степени открытия.
        rank = {"medium": 1, "high": 2, "critical": 3}
        assert rank[incident["peak_severity"]] >= rank[incident["severity"]]


# ------------------------------------------------------ вектор признаков (issue #24)

def _vector(stand: Stand, machine_id: str) -> dict:
    """Последний сохранённый вектор признаков агрегата."""
    return stand.hourly(machine_id)[-1]


def test_age_features_use_explicit_unknown_age_of_one_year(stand):
    # Возраст агрегата неизвестен → 1 год; поведение зафиксировано явно, чтобы
    # будущая смена источника возраста была видна (issue #24).
    m = _measurement("F-1", 36_500.0, 0)
    stand.register(m)
    stand.pipeline.ingest(m)

    v = _vector(stand, "F-1")
    assert v["Machine_Age_years"] == 1
    assert v["Days_Since_Install"] == 365
    assert v["Hours_per_Year"] == 36_500.0
    # Maint_Freq_days = 365 / (3 + 1); просрочки нет: 30 < 1.5 * 91.25
    assert v["Maint_Freq_days"] == pytest.approx(91.25)
    assert v["Maintenance_Overdue"] == 0


def test_maintenance_overdue_follows_threshold_of_one_and_a_half_intervals(stand):
    fresh = _measurement("F-2", NORMAL, 0)
    overdue = fresh.model_copy(update={"machine_id": "F-3",
                                       "last_maintenance_days_ago": 140})
    stand.register(fresh, overdue)
    stand.pipeline.ingest(fresh)
    stand.pipeline.ingest(overdue)

    assert _vector(stand, "F-2")["Maintenance_Overdue"] == 0
    # 140 > 1.5 * 91.25 = 136.9
    assert _vector(stand, "F-3")["Maintenance_Overdue"] == 1


def test_missing_mnar_sensor_is_filled_with_median_and_flagged(stand):
    m = _measurement("M-1", NORMAL, 0)
    stand.register(m)
    stand.pipeline.ingest(m)

    v = _vector(stand, "M-1")
    assert (v["Laser_Intensity"], v["Laser_Intensity_available"]) == (5000.0, 0)
    assert (v["Hydraulic_Pressure_bar"], v["Hydraulic_Pressure_bar_available"]) == (150.0, 0)
    assert (v["Coolant_Flow_L_min"], v["Coolant_Flow_L_min_available"]) == (40.0, 0)
    assert (v["Heat_Index"], v["Heat_Index_available"]) == (70.0, 0)


def test_present_mnar_sensor_is_window_mean_and_flagged(stand):
    first = _measurement("M-2", NORMAL, 0).model_copy(update={"laser_intensity": 4000.0})
    second = _measurement("M-2", NORMAL, 0).model_copy(update={
        "laser_intensity": 6000.0, "timestamp": _T0 + timedelta(minutes=10)})
    stand.register(first)
    stand.pipeline.ingest(first)
    stand.pipeline.ingest(second)

    v = _vector(stand, "M-2")
    assert (v["Laser_Intensity"], v["Laser_Intensity_available"]) == (5000.0, 1)
    assert v["Heat_Index_available"] == 0


def test_measurement_older_than_window_does_not_affect_vector(stand):
    old = _measurement("W-1", 10_000.0, 0)
    recent = _measurement("W-1", 30_000.0, 0).model_copy(update={
        "timestamp": _T0 + timedelta(hours=2)})
    inside = _measurement("W-1", 50_000.0, 0).model_copy(update={
        "timestamp": _T0 + timedelta(hours=2, minutes=10)})
    stand.register(old)
    for m in (old, recent, inside):
        stand.pipeline.ingest(m)

    # В окне только recent и inside: старое измерение на 2 ч за границей.
    assert _vector(stand, "W-1")["Operational_Hours"] == 40_000.0


def test_vectors_of_live_stream_and_history_are_identical(stand, second_stand):
    ms = _series("V-1", [NORMAL, MEDIUM, HIGH])
    stand.register(*ms)
    stand.pipeline.ingest_history(ms)
    _feed_one_by_one(second_stand, ms)

    got, want = stand.hourly("V-1"), second_stand.hourly("V-1")
    assert len(got) == len(want) == 3
    assert all(len(v) == 67 for v in got)
    assert got == want


def test_too_few_samples_buffers_without_prediction_or_hourly(ml, tmp_path):
    stand = Stand(ml, tmp_path, min_samples=3)
    close = [_measurement("S-1", NORMAL, 0).model_copy(
        update={"timestamp": _T0 + timedelta(minutes=10 * i)}) for i in range(3)]
    stand.register(*close)

    assert stand.pipeline.ingest(close[0]) is None
    assert stand.pipeline.ingest(close[1]) is None
    assert stand.predictions("S-1") == [] and stand.hourly("S-1") == []

    assert stand.pipeline.ingest(close[2]) is not None
    assert len(stand.predictions("S-1")) == len(stand.hourly("S-1")) == 1
