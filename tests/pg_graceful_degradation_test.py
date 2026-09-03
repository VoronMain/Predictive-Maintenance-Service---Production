# -*- coding: utf-8 -*-
"""
pg_graceful_degradation_test.py — регрессионный тест мягкой деградации
производственного бэкенда PostgreSQL при отсутствии расширения TimescaleDB
(issue #12, отложено из спецификации #1).

Ранее сценарий «managed-PostgreSQL без TimescaleDB» (например, Railway
HOBBY, где собственный образ timescale/timescaledb уходит в краш-луп по
OOM) проверялся только пост-фактум на живом стенде. Здесь через
testcontainers поднимается обычный PostgreSQL без расширения, и
проверяется, что:

  * PostgresDatabase(dsn) конструируется без исключения, инициализация
    схемы (_initialize_schema) проходит до конца, наружу ничего не летит;
  * флаг self.timescaledb_available выставлен в False, расширение в БД
    действительно отсутствует;
  * таблицы временных рядов telemetry_raw / telemetry_hourly / predictions
    созданы как обычные таблицы, а не гипертаблицы (нет схемы
    _timescaledb_catalog, relkind = 'r');
  * базовые операции чтения-записи работают: upsert оборудования, запись и
    чтение предсказаний, пакетный засев, агрегатные выборки.

Тест требует Docker. При его отсутствии (нет пакета testcontainers, не
запущен демон Docker) модуль пропускается целиком — по тому же принципу
мягкого скипа, что и остальные внешние интеграционные проверки.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Нет пакета testcontainers — пропускаем весь модуль.
_pg = pytest.importorskip(
    "testcontainers.postgres",
    reason="testcontainers не установлен — интеграционный тест PostgreSQL пропущен",
)
PostgresContainer = _pg.PostgresContainer

from app.schema import (  # noqa: E402
    EquipmentRecord,
    PredictionRecord,
    TelemetryMeasurement,
)

# Обычный PostgreSQL — заведомо без расширения TimescaleDB.
_IMAGE = "postgres:16-alpine"


def _libpq_dsn(container: "PostgresContainer") -> str:
    """URL от testcontainers → строка подключения в формате psycopg/libpq.

    get_connection_url() отдаёт SQLAlchemy-URL вида
    postgresql+psycopg2://user:pass@host:port/db; psycopg3 понимает
    postgresql://, поэтому суффикс драйвера убираем.
    """
    url = container.get_connection_url()
    return re.sub(r"^postgresql\+[a-z0-9]+://", "postgresql://", url)


@pytest.fixture(scope="module")
def pg_dsn():
    """Поднимает обычный PostgreSQL в контейнере и отдаёт DSN.

    Если Docker недоступен (демон не запущен, нет прав, нет самого
    Docker) — контейнер не стартует, и модуль мягко пропускается.
    """
    try:
        container = PostgresContainer(_IMAGE)
        container.start()
    except Exception as exc:  # noqa: BLE001 — любую ошибку старта трактуем как «нет Docker»
        pytest.skip(f"Docker недоступен — тест деградации PostgreSQL пропущен: {exc}")
        return

    try:
        yield _libpq_dsn(container)
    finally:
        container.stop()


@pytest.fixture()
def db(pg_dsn):
    """Свежий PostgresDatabase на «голом» PostgreSQL для CRUD-проверок."""
    from app.pg_database import PostgresDatabase

    instance = PostgresDatabase(pg_dsn)
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------- #
# Конструирование и инициализация схемы без TimescaleDB
# --------------------------------------------------------------- #
def test_constructs_without_timescaledb(pg_dsn):
    """PostgresDatabase(dsn) на PostgreSQL без расширения:
    конструктор отрабатывает, схема инициализируется, исключение наружу
    не выходит, флаг timescaledb_available == False."""
    from app.pg_database import PostgresDatabase

    instance = PostgresDatabase(pg_dsn)
    try:
        assert instance.timescaledb_available is False
        with instance._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM pg_extension WHERE extname = 'timescaledb'")
            assert cur.fetchone()["n"] == 0
    finally:
        instance.close()


def test_reinitialization_is_idempotent(pg_dsn):
    """Повторное построение бэкенда на той же БД (схема уже создана)
    также проходит без исключений — CREATE TABLE IF NOT EXISTS и
    охраняемые миграции идемпотентны."""
    from app.pg_database import PostgresDatabase

    first = PostgresDatabase(pg_dsn)
    first.close()
    second = PostgresDatabase(pg_dsn)
    try:
        assert second.timescaledb_available is False
    finally:
        second.close()


@pytest.mark.parametrize("table", ["telemetry_raw", "telemetry_hourly", "predictions"])
def test_time_series_tables_are_plain_tables(db, table):
    """Таблицы временных рядов — обычные таблицы PostgreSQL (relkind='r'),
    гипертаблицы TimescaleDB не создаются."""
    with db._conn.cursor() as cur:
        cur.execute("SELECT relkind FROM pg_class WHERE relname = %s", (table,))
        row = cur.fetchone()
    assert row is not None, f"таблица {table} не создана"
    assert row["relkind"] == "r"


def test_no_timescaledb_catalog_schema(db):
    """Схемы служебного каталога TimescaleDB в БД нет."""
    with db._conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.schemata "
            "WHERE schema_name = '_timescaledb_catalog'"
        )
        assert cur.fetchone()["n"] == 0


def test_retention_policy_call_is_noop_without_timescaledb(db):
    """apply_retention_policy на обычном PostgreSQL — тихий no-op,
    обращения к несуществующему timescaledb_information.jobs нет."""
    db.apply_retention_policy()  # не должно бросать


# --------------------------------------------------------------- #
# Базовые операции чтения-записи
# --------------------------------------------------------------- #
def _measurement(machine_id: str, ts: datetime, **over) -> TelemetryMeasurement:
    payload = dict(
        machine_id=machine_id,
        machine_type="Furnace",
        timestamp=ts,
        operational_hours=1234.0,
        temperature_c=61.0,
        vibration_mms=11.0,
        sound_db=92.0,
        oil_level_pct=64.0,
        coolant_level_pct=71.0,
        power_consumption_kw=270.0,
        last_maintenance_days_ago=10,
        maintenance_history_count=6,
        failure_history_count=0,
        ai_supervision=True,
        error_codes_last_30_days=2,
        ai_override_events=1,
    )
    payload.update(over)
    return TelemetryMeasurement(**payload)


def test_equipment_upsert_and_list(db):
    db.upsert_equipment(EquipmentRecord(
        machine_id="M-1", machine_type="Furnace",
        operational_hours=100.0, category="печи",
    ))
    db.upsert_equipment(EquipmentRecord(
        machine_id="M-2", machine_type="Boiler",
        operational_hours=200.0, category="котлы",
    ))
    # Повторный upsert обновляет запись, дубля не создаёт.
    db.upsert_equipment(EquipmentRecord(
        machine_id="M-1", machine_type="Furnace",
        operational_hours=150.0, category="печи",
    ))

    rows = db.list_equipment()
    by_id = {r["machine_id"]: r for r in rows}
    assert set(by_id) == {"M-1", "M-2"}
    assert by_id["M-1"]["operational_hours"] == 150.0


def test_raw_measurement_write_and_read(db):
    db.upsert_equipment(EquipmentRecord(machine_id="M-10", machine_type="Furnace"))
    now = datetime.now(timezone.utc)
    db.insert_raw_measurement(_measurement("M-10", now - timedelta(minutes=5)))
    db.insert_raw_measurement(_measurement("M-10", now, temperature_c=70.0))

    rows = db.latest_raw_measurements("M-10", limit=10)
    assert len(rows) == 2
    # Свежайшее измерение первым, метка времени — строка ISO 8601.
    assert rows[0]["temperature_c"] == 70.0
    datetime.fromisoformat(rows[0]["timestamp"])


def test_prediction_write_and_read(db):
    db.upsert_equipment(EquipmentRecord(machine_id="M-20", machine_type="Furnace"))
    now = datetime.now(timezone.utc)
    for i, prob in enumerate((0.1, 0.4, 0.7)):
        db.insert_prediction(PredictionRecord(
            machine_id="M-20",
            timestamp=now - timedelta(hours=2 - i),
            failure_probability=prob,
            failure_label=int(prob >= 0.33),
            remaining_useful_life_days=30.0 - i,
            threshold=0.33,
        ))

    assert db.has_predictions() is True

    latest = db.latest_predictions(limit=10)
    assert [p["failure_probability"] for p in latest] == [0.7, 0.4, 0.1]
    assert latest[0]["machine_type"] == "Furnace"

    history = db.predictions_history("M-20", limit=10)
    assert len(history) == 3

    per_machine = db.latest_prediction_per_machine()
    assert per_machine[0]["machine_id"] == "M-20"
    assert per_machine[0]["failure_probability"] == 0.7


def test_bulk_seed_and_aggregate_reads(db):
    db.upsert_equipment(EquipmentRecord(machine_id="M-30", machine_type="Furnace"))
    now = datetime.now(timezone.utc)
    telemetry_rows = [
        (
            "M-30",
            (now - timedelta(hours=h)).isoformat(),
            '{"temperature_c": %d, "vibration_mms": 10, "sound_db": 90, '
            '"oil_level_pct": 60, "coolant_level_pct": 70, '
            '"power_consumption_kw": 250}' % (60 + h),
        )
        for h in range(3)
    ]
    prediction_rows = [
        ("M-30", (now - timedelta(hours=h)).isoformat(), 0.2 + 0.1 * h, 0, 25.0, 0.33)
        for h in range(3)
    ]
    db.bulk_insert_for_seed(telemetry_rows, prediction_rows)

    averages = db.get_sensor_averages("M-30", days=7)
    assert averages["temperature_c"] == pytest.approx(61.0, abs=0.01)

    overview = db.all_machines_overview()
    m30 = next(r for r in overview if r["machine_id"] == "M-30")
    # Свежайшая запись (h=0): предсказание 0.2, температура 60.
    assert m30["failure_probability"] == pytest.approx(0.2)
    assert m30["temperature_c"] == pytest.approx(60.0)


def test_hourly_aggregate_write(db):
    db.upsert_equipment(EquipmentRecord(machine_id="M-40", machine_type="Furnace"))
    window_end = datetime.now(timezone.utc)
    db.insert_hourly_aggregate("M-40", window_end, {"temperature_c_mean": 61.5,
                                                    "vibration_mms_mean": float("nan")})
    with db._conn.cursor() as cur:
        cur.execute("SELECT features FROM telemetry_hourly WHERE machine_id = 'M-40'")
        row = cur.fetchone()
    assert row["features"]["temperature_c_mean"] == 61.5
    assert row["features"]["vibration_mms_mean"] is None  # NaN → None


def test_incident_lifecycle(db):
    db.upsert_equipment(EquipmentRecord(machine_id="M-50", machine_type="Furnace"))
    now = datetime.now(timezone.utc)
    incident_id = db.open_incident("M-50", now, 0.5, 12.0, 0.33, severity="high")
    assert db.get_open_incident("M-50")["id"] == incident_id

    db.update_open_incident(incident_id, 0.8, 6.0, peak_severity="critical")
    db.close_incident(incident_id, now + timedelta(hours=1))
    assert db.get_open_incident("M-50") is None

    summary = db.incidents_summary()
    assert summary["total"] == 1
    assert summary["closed"] == 1

    incidents = db.list_incidents(machine_type="Furnace")
    assert incidents[0]["peak_probability"] == 0.8


def test_notification_settings_roundtrip(db):
    saved = db.save_notification_settings(
        email_enabled=True, sms_enabled=True, push_enabled=False,
        email="ops@example.local", phone="+70000000000", failure_threshold=0.42,
    )
    assert saved["sms_enabled"] is True
    assert saved["failure_threshold"] == 0.42

    again = db.get_notification_settings()
    assert again["email"] == "ops@example.local"
    assert again["failure_threshold"] == 0.42
