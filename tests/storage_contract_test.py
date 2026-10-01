# -*- coding: utf-8 -*-
"""
storage_contract_test.py — контракт хранилища (issue #27): один набор
тестов, который прогоняется на каждом adapter интерфейса DatabaseProtocol.

Seam — интерфейс хранилища. Тест видит только то, что наблюдаемо через
методы adapter: что записали — то и прочитали, в каком формате и порядке,
что отдаётся на пустой базе. Таблицы напрямую не читаются, SQL не
проверяется.

Адаптеры подключаются в словаре _BACKENDS (имя → фабрика, получающая
pytest request и каталог tmp_path). SQLite идёт всегда; PostgreSQL —
в контейнере testcontainers, пропускается с причиной без Docker (в CI
SPA_REQUIRE_PG_TESTS=1 превращает пропуск в ошибку). Чтобы проверить
третий adapter (например, in-memory), достаточно добавить запись в
_BACKENDS: ни один тест ниже менять не нужно.

Контракт формата меток времени: любая метка в ответе хранилища — строка
ISO 8601 в UTC с суффиксом +00:00 (адаптер сам приводит значение, вызывающий
код ничего не нормализует).

Всё, что специфично для PostgreSQL (работа без TimescaleDB), живёт в
pg_graceful_degradation_test.py.
"""
from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import settings  # noqa: E402
from app.db_base import DatabaseProtocol  # noqa: E402
from app.notification_settings import load_notification_settings  # noqa: E402
from app.schema import (  # noqa: E402
    EquipmentRecord,
    HourlyAggregateRecord,
    PredictionRecord,
    TelemetryMeasurement,
)
from app.seeder import is_already_seeded  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_SENSORS = ("temperature_c", "vibration_mms", "sound_db", "oil_level_pct",
            "coolant_level_pct", "power_consumption_kw")


# --------------------------------------------------------------- #
# Подключение adapters
# --------------------------------------------------------------- #
def _make_sqlite(request, tmp_path):
    from app.database import SQLiteDatabase

    return SQLiteDatabase(tmp_path / "spa.db")


def _make_postgres(request, tmp_path):
    from app.pg_database import PostgresDatabase

    # Один контейнер на модуль; между тестами — очистка данных.
    instance = PostgresDatabase(request.getfixturevalue("pg_dsn"))
    with instance._transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE equipment, telemetry_raw, telemetry_hourly, predictions, "
            "alert_thresholds, incidents_log, alerts_log, notification_settings "
            "RESTART IDENTITY CASCADE"
        )
    return instance


_BACKENDS = {"sqlite": _make_sqlite, "postgres": _make_postgres}


@pytest.fixture(params=list(_BACKENDS))
def db(request, tmp_path):
    """Чистый adapter хранилища; параметризован по бэкенду."""
    instance = _BACKENDS[request.param](request, tmp_path)
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------- #
# Конструкторы тестовых данных
# --------------------------------------------------------------- #
def _equipment(db, machine_id="M-1", machine_type="Furnace", hours=100.0, category=""):
    db.upsert_equipment(EquipmentRecord(
        machine_id=machine_id, machine_type=machine_type,
        operational_hours=hours, category=category,
    ))


def _measurement(machine_id, ts, **over) -> TelemetryMeasurement:
    payload = dict(
        machine_id=machine_id, machine_type="Furnace", timestamp=ts,
        operational_hours=1234.0, temperature_c=61.0, vibration_mms=11.0,
        sound_db=92.0, oil_level_pct=64.0, coolant_level_pct=71.0,
        power_consumption_kw=270.0, last_maintenance_days_ago=10,
        maintenance_history_count=6, failure_history_count=0,
        ai_supervision=True, error_codes_last_30_days=2, ai_override_events=1,
    )
    payload.update(over)
    return TelemetryMeasurement(**payload)


def _prediction(machine_id, ts, prob=0.5, rul=30.0, threshold=0.33) -> PredictionRecord:
    return PredictionRecord(
        machine_id=machine_id, timestamp=ts, failure_probability=prob,
        failure_label=int(prob >= threshold), remaining_useful_life_days=rul,
        threshold=threshold,
    )


# --------------------------------------------------------------- #
# Состав interface
# --------------------------------------------------------------- #
def _public_methods(cls) -> set[str]:
    return {name for name, member in inspect.getmembers(cls, callable)
            if not name.startswith("_")}


def _protocol_methods() -> set[str]:
    return {name for name in vars(DatabaseProtocol)
            if not name.startswith("_") and callable(getattr(DatabaseProtocol, name))}


@pytest.mark.parametrize("module, cls_name", [
    ("app.database", "SQLiteDatabase"),
    ("app.pg_database", "PostgresDatabase"),
])
def test_interface_lists_exactly_what_adapters_expose(module, cls_name):
    """Interface описывает ровно публичные методы adapter: ни мёртвых
    методов вне контракта, ни методов контракта без реализации."""
    import importlib

    cls = getattr(importlib.import_module(module), cls_name)
    assert _public_methods(cls) == _protocol_methods()


def test_adapter_satisfies_protocol(db):
    assert isinstance(db, DatabaseProtocol)


# --------------------------------------------------------------- #
# Справочник агрегатов
# --------------------------------------------------------------- #
def test_equipment_empty(db):
    assert db.list_equipment() == []


def test_equipment_register_and_list_sorted(db):
    _equipment(db, "M-2", "Boiler", 200.0, "котлы")
    _equipment(db, "M-1", "Furnace", 100.0, "печи")

    assert db.list_equipment() == [
        {"machine_id": "M-1", "machine_type": "Furnace",
         "operational_hours": 100.0, "category": "печи"},
        {"machine_id": "M-2", "machine_type": "Boiler",
         "operational_hours": 200.0, "category": "котлы"},
    ]


def test_equipment_update_keeps_category_when_new_one_is_empty(db):
    _equipment(db, "M-1", hours=100.0, category="печи")
    _equipment(db, "M-1", hours=150.0, category="")  # как живой поток: без категории

    (row,) = db.list_equipment()
    assert row["operational_hours"] == 150.0
    assert row["category"] == "печи"


def test_equipment_update_replaces_category_with_non_empty(db):
    _equipment(db, "M-1", category="печи")
    _equipment(db, "M-1", category="термообработка")

    (row,) = db.list_equipment()
    assert row["category"] == "термообработка"


# --------------------------------------------------------------- #
# Сырые измерения, средние по датчикам, часовые агрегаты
# --------------------------------------------------------------- #
def test_raw_measurements_empty(db):
    assert db.latest_raw_measurements("nobody") == []


def test_raw_measurements_newest_first_with_limit_and_utc_timestamps(db):
    _equipment(db)
    for i in range(3):
        db.insert_raw_measurement(
            _measurement("M-1", T0 + timedelta(minutes=i), temperature_c=60.0 + i))

    rows = db.latest_raw_measurements("M-1", limit=2)

    assert [r["temperature_c"] for r in rows] == [62.0, 61.0]
    assert rows[0]["timestamp"] == "2026-01-01T12:02:00+00:00"
    assert rows[0]["machine_id"] == "M-1"


def test_raw_measurements_are_per_machine(db):
    _equipment(db, "M-1")
    _equipment(db, "M-2")
    db.insert_raw_measurement(_measurement("M-1", T0))
    db.insert_raw_measurement(_measurement("M-2", T0, temperature_c=99.0))

    assert [r["temperature_c"] for r in db.latest_raw_measurements("M-2")] == [99.0]


def test_sensor_averages_on_empty_data_are_none(db):
    _equipment(db)
    assert db.get_sensor_averages("M-1") == {k: None for k in _SENSORS}


def test_sensor_averages_cover_window_and_round_to_two_digits(db):
    _equipment(db)
    now = datetime.now(UTC)
    for i, temp in enumerate((1.0, 2.0, 2.0)):
        db.insert_raw_measurement(
            _measurement("M-1", now - timedelta(hours=i + 1), temperature_c=temp))
    # За пределами окна в 7 суток — в среднее не входит.
    db.insert_raw_measurement(
        _measurement("M-1", now - timedelta(days=30), temperature_c=150.0))

    averages = db.get_sensor_averages("M-1", days=7)

    assert set(averages) == set(_SENSORS)
    assert averages["temperature_c"] == 1.67
    assert averages["vibration_mms"] == 11.0


def test_hourly_aggregate_writes_accept_nan_features(db):
    _equipment(db)
    db.insert_hourly_aggregate("M-1", T0, {"temperature_c_mean": 61.5,
                                           "vibration_mms_mean": float("nan")})
    db.insert_history([], [], [HourlyAggregateRecord(
        machine_id="M-1", window_end=T0 + timedelta(hours=1),
        features={"temperature_c_mean": 62.0, "vibration_mms_mean": float("nan")})])


# --------------------------------------------------------------- #
# Предсказания и обзор
# --------------------------------------------------------------- #
def test_has_predictions_flips_after_first_prediction(db):
    _equipment(db)
    assert db.has_predictions() is False

    db.insert_prediction(_prediction("M-1", T0))

    assert db.has_predictions() is True


def test_latest_predictions_empty(db):
    assert db.latest_predictions() == []


def test_latest_predictions_newest_first_with_machine_type_and_limit(db):
    _equipment(db, "M-1", "Furnace")
    _equipment(db, "M-2", "Boiler")
    db.insert_prediction(_prediction("M-1", T0, prob=0.1))
    db.insert_prediction(_prediction("M-2", T0 + timedelta(hours=2), prob=0.7))
    db.insert_prediction(_prediction("M-1", T0 + timedelta(hours=1), prob=0.4))

    rows = db.latest_predictions(limit=2)

    assert [(r["machine_id"], r["failure_probability"]) for r in rows] == [
        ("M-2", 0.7), ("M-1", 0.4)]
    assert rows[0]["machine_type"] == "Boiler"
    assert rows[0]["timestamp"] == "2026-01-01T14:00:00+00:00"
    assert rows[0]["failure_label"] == 1
    assert isinstance(rows[0]["failure_label"], int)
    assert isinstance(rows[0]["remaining_useful_life_days"], float)


def test_predictions_history_per_machine_newest_first(db):
    _equipment(db, "M-1")
    _equipment(db, "M-2")
    for i, prob in enumerate((0.1, 0.4, 0.7)):
        db.insert_prediction(_prediction("M-1", T0 + timedelta(hours=i), prob=prob))
    db.insert_prediction(_prediction("M-2", T0, prob=0.9))

    history = db.predictions_history("M-1", limit=2)

    assert [p["failure_probability"] for p in history] == [0.7, 0.4]
    assert history[0]["timestamp"] == "2026-01-01T14:00:00+00:00"
    assert set(history[0]) == {"timestamp", "failure_probability", "failure_label",
                               "remaining_useful_life_days", "threshold"}
    assert db.predictions_history("nobody") == []


def test_overview_empty(db):
    assert db.all_machines_overview() == []


def test_overview_includes_machines_without_data_and_sorts_by_risk(db):
    _equipment(db, "M-A", "Furnace", 10.0, "печи")
    _equipment(db, "M-B", "Boiler", 20.0, "котлы")
    _equipment(db, "M-D", "Pump")
    _equipment(db, "M-C", "Pump")
    # M-A: два измерения (берётся последнее) и два предсказания (берётся последнее).
    db.insert_raw_measurement(_measurement("M-A", T0, temperature_c=50.0))
    db.insert_raw_measurement(_measurement("M-A", T0 + timedelta(hours=1), temperature_c=55.0))
    db.insert_prediction(_prediction("M-A", T0, prob=0.9))
    db.insert_prediction(_prediction("M-A", T0 + timedelta(hours=1), prob=0.2, rul=40.0))
    db.insert_prediction(_prediction("M-B", T0, prob=0.6))

    rows = db.all_machines_overview()

    # Сначала по убыванию вероятности, затем без предсказаний — по machine_id.
    assert [r["machine_id"] for r in rows] == ["M-B", "M-A", "M-C", "M-D"]
    by_id = {r["machine_id"]: r for r in rows}

    a = by_id["M-A"]
    assert a["failure_probability"] == 0.2
    assert a["remaining_useful_life_days"] == 40.0
    assert a["timestamp"] == "2026-01-01T13:00:00+00:00"
    assert a["temperature_c"] == 55.0
    assert a["machine_type"] == "Furnace"
    assert a["operational_hours"] == 10.0
    assert a["category"] == "печи"
    assert isinstance(a["failure_label"], int)

    b = by_id["M-B"]  # предсказание есть, телеметрии нет
    assert b["failure_probability"] == 0.6
    assert all(b[k] is None for k in _SENSORS)

    c = by_id["M-C"]  # ни предсказаний, ни телеметрии
    expected_empty = ("timestamp", "failure_probability", "failure_label",
                      "remaining_useful_life_days", "threshold", *_SENSORS)
    assert all(c[k] is None for k in expected_empty)
    assert set(c) == set(a)


# --------------------------------------------------------------- #
# Метки времени
# --------------------------------------------------------------- #
def test_timestamps_from_other_timezone_are_returned_in_utc(db):
    _equipment(db)
    plus5 = timezone(timedelta(hours=5))
    local = datetime(2026, 1, 1, 17, 0, 0, tzinfo=plus5)  # == 12:00 UTC

    db.insert_prediction(_prediction("M-1", local))
    db.insert_raw_measurement(_measurement("M-1", local))
    db.open_incident("M-1", local, 0.5, 10.0, 0.33)
    alert_id = db.insert_alert(None, "M-1", local, "a@b", "s", "b", "email", "sent")

    assert db.predictions_history("M-1")[0]["timestamp"] == "2026-01-01T12:00:00+00:00"
    assert db.latest_raw_measurements("M-1")[0]["timestamp"] == "2026-01-01T12:00:00+00:00"
    assert db.get_open_incident("M-1")["opened_at"] == "2026-01-01T12:00:00+00:00"
    assert db.get_alert(alert_id)["sent_at"] == "2026-01-01T12:00:00+00:00"


def test_mixed_timezones_sort_by_instant_not_by_text(db):
    _equipment(db)
    plus5 = timezone(timedelta(hours=5))
    # 11:00 UTC записано как 16:00+05:00 — текстом «больше» 12:00+00:00, по времени «меньше».
    db.insert_prediction(_prediction("M-1", datetime(2026, 1, 1, 16, 0, tzinfo=plus5), prob=0.1))
    db.insert_prediction(_prediction("M-1", datetime(2026, 1, 1, 12, 0, tzinfo=UTC), prob=0.2))

    assert [p["failure_probability"] for p in db.predictions_history("M-1")] == [0.2, 0.1]


# --------------------------------------------------------------- #
# Пакетная запись истории и признак засева
# --------------------------------------------------------------- #
def _history(machine_id="M-1", n=3):
    measurements = [_measurement(machine_id, T0 + timedelta(hours=h),
                                 temperature_c=60.0 + h) for h in range(n)]
    predictions = [_prediction(machine_id, T0 + timedelta(hours=h), prob=0.1 * (h + 1))
                   for h in range(n)]
    hourly = [HourlyAggregateRecord(
        machine_id=machine_id, window_end=T0 + timedelta(hours=h),
        features={"temperature_c_mean": 60.0 + h}) for h in range(n)]
    return measurements, predictions, hourly


def test_seeded_flag_on_empty_and_filled_database(db):
    assert is_already_seeded(db) is False

    _equipment(db)
    db.insert_history(*_history())

    assert is_already_seeded(db) is True


def test_insert_history_is_readable_through_the_same_methods_as_live_stream(db):
    _equipment(db)
    db.insert_history(*_history(n=3))

    raw = db.latest_raw_measurements("M-1", limit=10)
    history = db.predictions_history("M-1", limit=10)

    assert [r["temperature_c"] for r in raw] == [62.0, 61.0, 60.0]
    assert raw[0]["timestamp"] == "2026-01-01T14:00:00+00:00"
    assert [p["failure_probability"] for p in history] == pytest.approx([0.3, 0.2, 0.1])


def test_insert_history_with_empty_batches_is_a_noop(db):
    db.insert_history([], [], [])
    assert db.has_predictions() is False


def test_insert_history_is_idempotent_by_machine_and_time(db):
    _equipment(db)
    db.insert_history(*_history(n=3))
    db.insert_history(*_history(n=3))  # повторный засев той же истории

    assert len(db.latest_raw_measurements("M-1", limit=100)) == 3
    assert len(db.predictions_history("M-1", limit=100)) == 3


def test_insert_history_repeat_adds_only_new_points(db):
    _equipment(db)
    db.insert_history(*_history(n=2))
    db.insert_history(*_history(n=4))

    assert len(db.latest_raw_measurements("M-1", limit=100)) == 4
    assert len(db.predictions_history("M-1", limit=100)) == 4


# --------------------------------------------------------------- #
# Инциденты
# --------------------------------------------------------------- #
def test_incidents_empty(db):
    assert db.get_open_incident("M-1") is None
    assert db.list_incidents() == []
    assert db.incidents_summary() == {
        "total": 0, "open": 0, "closed": 0, "open_by_severity": {}}


def test_incident_open_and_find_open(db):
    _equipment(db)

    incident_id = db.open_incident("M-1", T0, 0.5, 12.0, 0.33, severity="high")

    assert isinstance(incident_id, int)
    row = db.get_open_incident("M-1")
    assert row["id"] == incident_id
    assert row["status"] == "open"
    assert row["opened_at"] == "2026-01-01T12:00:00+00:00"
    assert row["closed_at"] is None
    assert (row["opened_probability"], row["peak_probability"]) == (0.5, 0.5)
    assert (row["opened_rul_days"], row["min_rul_days"]) == (12.0, 12.0)
    assert (row["severity"], row["peak_severity"]) == ("high", "high")
    assert row["threshold"] == 0.33
    assert db.get_open_incident("other") is None


def test_incident_severity_defaults_to_medium(db):
    _equipment(db)
    db.open_incident("M-1", T0, 0.5, 12.0, 0.33)

    row = db.get_open_incident("M-1")
    assert (row["severity"], row["peak_severity"]) == ("medium", "medium")


def test_incident_update_tracks_peak_probability_and_minimum_rul(db):
    _equipment(db)
    incident_id = db.open_incident("M-1", T0, 0.5, 12.0, 0.33, severity="high")

    db.update_open_incident(incident_id, 0.8, 6.0, peak_severity="critical")
    db.update_open_incident(incident_id, 0.6, 9.0, peak_severity="critical")  # хуже не стало

    row = db.get_open_incident("M-1")
    assert row["peak_probability"] == 0.8
    assert row["min_rul_days"] == 6.0
    assert row["opened_probability"] == 0.5
    assert row["peak_severity"] == "critical"


def test_incident_close(db):
    _equipment(db)
    incident_id = db.open_incident("M-1", T0, 0.5, 12.0, 0.33)

    db.close_incident(incident_id, T0 + timedelta(hours=1))

    assert db.get_open_incident("M-1") is None
    (row,) = db.list_incidents()
    assert row["status"] == "closed"
    assert row["closed_at"] == "2026-01-01T13:00:00+00:00"


def test_incident_list_filters_order_and_limit(db):
    _equipment(db, "M-1", "Furnace")
    _equipment(db, "M-2", "Boiler")
    first = db.open_incident("M-1", T0, 0.5, 12.0, 0.33)
    db.close_incident(first, T0 + timedelta(hours=1))
    db.open_incident("M-2", T0 + timedelta(hours=2), 0.6, 8.0, 0.33)
    db.open_incident("M-1", T0 + timedelta(hours=3), 0.7, 5.0, 0.33)

    everything = db.list_incidents()
    assert [i["opened_at"] for i in everything] == [
        "2026-01-01T15:00:00+00:00", "2026-01-01T14:00:00+00:00", "2026-01-01T12:00:00+00:00"]
    assert everything[0]["machine_type"] == "Furnace"
    assert set(everything[0]) == {
        "id", "machine_id", "machine_type", "opened_at", "closed_at",
        "opened_probability", "peak_probability", "opened_rul_days", "min_rul_days",
        "threshold", "severity", "peak_severity", "status"}

    assert len(db.list_incidents(status="open")) == 2
    assert len(db.list_incidents(status="closed")) == 1
    assert [i["machine_id"] for i in db.list_incidents(machine_type="Boiler")] == ["M-2"]
    assert [i["machine_id"] for i in db.list_incidents(status="open", machine_type="Furnace")] == ["M-1"]
    assert len(db.list_incidents(limit=1)) == 1


def test_incidents_summary_counts_by_status_and_open_severity(db):
    _equipment(db, "M-1")
    _equipment(db, "M-2")
    _equipment(db, "M-3")
    closed = db.open_incident("M-1", T0, 0.5, 12.0, 0.33, severity="high")
    db.close_incident(closed, T0 + timedelta(hours=1))
    db.open_incident("M-2", T0, 0.6, 8.0, 0.33, severity="critical")
    db.open_incident("M-3", T0, 0.7, 5.0, 0.33, severity="critical")

    assert db.incidents_summary() == {
        "total": 3, "open": 2, "closed": 1, "open_by_severity": {"critical": 2}}


# --------------------------------------------------------------- #
# Журнал оповещений
# --------------------------------------------------------------- #
def _alert(db, sent_at, *, machine_id="M-1", status="sent", incident_id=None, **extra):
    return db.insert_alert(incident_id, machine_id, sent_at, "ops@example.local",
                           "Тема", "Тело письма", "email", status, **extra)


def test_alerts_empty(db):
    assert db.get_alerts_count() == 0
    assert db.list_alerts() == []
    assert db.get_alert(1) is None
    assert db.last_alert_for_machine("M-1") is None


def test_alert_insert_and_get_by_id(db):
    _equipment(db)
    incident_id = db.open_incident("M-1", T0, 0.5, 12.0, 0.33)

    alert_id = _alert(db, T0, incident_id=incident_id, severity="high",
                      group_key="g1", grouped_count=3)

    assert isinstance(alert_id, int)
    row = db.get_alert(alert_id)
    assert row == {
        "id": alert_id, "incident_id": incident_id, "machine_id": "M-1",
        "sent_at": "2026-01-01T12:00:00+00:00", "recipient": "ops@example.local",
        "subject": "Тема", "body": "Тело письма", "channel": "email",
        "severity": "high", "group_key": "g1", "grouped_count": 3,
        "status": "sent", "error": None,
    }


def test_alert_defaults(db):
    _equipment(db)
    row = db.get_alert(_alert(db, T0, status="failed", error="smtp down"))

    assert row["incident_id"] is None
    assert (row["severity"], row["group_key"], row["grouped_count"]) == ("medium", None, 1)
    assert row["error"] == "smtp down"


def test_alert_unknown_id_is_none(db):
    _equipment(db)
    _alert(db, T0)
    assert db.get_alert(10_000) is None


def test_alert_list_newest_first_without_body_and_with_limit(db):
    _equipment(db)
    for h in range(3):
        _alert(db, T0 + timedelta(hours=h))

    rows = db.list_alerts(limit=2)

    assert [r["sent_at"] for r in rows] == [
        "2026-01-01T14:00:00+00:00", "2026-01-01T13:00:00+00:00"]
    assert "body" not in rows[0]
    assert set(rows[0]) == {"id", "incident_id", "machine_id", "sent_at", "recipient",
                            "subject", "channel", "severity", "group_key",
                            "grouped_count", "status", "error"}


def test_alerts_count_includes_only_sent(db):
    _equipment(db)
    _alert(db, T0)
    _alert(db, T0 + timedelta(hours=1))
    _alert(db, T0 + timedelta(hours=2), status="failed")

    assert db.get_alerts_count() == 2


def test_last_alert_for_machine_is_latest_sent_one(db):
    _equipment(db, "M-1")
    _equipment(db, "M-2")
    _alert(db, T0)
    latest_sent = _alert(db, T0 + timedelta(hours=1))
    _alert(db, T0 + timedelta(hours=2), status="failed")
    _alert(db, T0 + timedelta(hours=3), machine_id="M-2")

    row = db.last_alert_for_machine("M-1")

    assert row["id"] == latest_sent
    assert row["sent_at"] == "2026-01-01T13:00:00+00:00"
    assert db.last_alert_for_machine("nobody") is None


# --------------------------------------------------------------- #
# Настройки оповещений
# --------------------------------------------------------------- #
def _save(db, **over):
    values = dict(email_enabled=True, sms_enabled=False, push_enabled=False,
                  email="ops@example.local", phone="", failure_threshold=0.42)
    values.update(over)
    return db.save_notification_settings(**values)


def test_notification_settings_absent_on_empty_database(db):
    # Adapter значений по умолчанию не знает: «нет настроек» — это None.
    assert db.get_notification_settings() is None


def test_notification_settings_save_and_read_back_with_types(db):
    saved = _save(db, sms_enabled=True, phone="+70000000000")

    for result in (saved, db.get_notification_settings()):
        assert result["email_enabled"] is True
        assert result["sms_enabled"] is True
        assert result["push_enabled"] is False
        assert result["email"] == "ops@example.local"
        assert result["phone"] == "+70000000000"
        assert result["failure_threshold"] == 0.42
        updated_at = datetime.fromisoformat(result["updated_at"])
        assert updated_at.utcoffset() == timedelta(0)
        assert result["updated_at"].endswith("+00:00")


def test_notification_settings_resave_overwrites_single_record(db):
    _save(db, email="first@example.local", failure_threshold=0.42)
    _save(db, email="second@example.local", email_enabled=False, failure_threshold=0.5)

    result = db.get_notification_settings()
    assert result["email"] == "second@example.local"
    assert result["email_enabled"] is False
    assert result["failure_threshold"] == 0.5


def test_notification_defaults_on_empty_database_come_from_config(db):
    result = load_notification_settings(db)

    assert result == {
        "email_enabled": True, "sms_enabled": False, "push_enabled": False,
        "email": settings.SMTP_TO, "phone": "",
        "failure_threshold": settings.FAILURE_THRESHOLD, "updated_at": None,
    }


def test_notification_settings_saved_values_win_over_defaults(db):
    _save(db, email="ops@example.local", failure_threshold=0.42)

    result = load_notification_settings(db)

    assert result["email"] == "ops@example.local"
    assert result["failure_threshold"] == 0.42
