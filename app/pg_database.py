# -*- coding: utf-8 -*-
"""
pg_database.py — производственный бэкенд подсистемы хранения данных на
базе PostgreSQL, опционально с расширением TimescaleDB.

Бэкенд реализует тот же программный интерфейс, что и резервный бэкенд
SQLite (модуль app/database.py), и предназначен для замены последнего в
производственном контуре без изменения прикладного кода подсистем.

Расширение TimescaleDB подключается отдельным охраняемым шагом
инициализации (_setup_timescaledb_extension) и не является обязательным:
на управляемом PostgreSQL без этого расширения (например, managed-PostgreSQL,
где собственный образ timescale/timescaledb недоступен)
бэкенд деградирует до обычных таблиц PostgreSQL, не теряя работоспособность.
Доступность расширения отражена флагом self.timescaledb_available.

Ключевые отличия при доступном TimescaleDB:

  * таблицы временных рядов telemetry_raw, telemetry_hourly и predictions
    преобразуются в гипертаблицы TimescaleDB средствами create_hypertable,
    что обеспечивает автоматическое секционирование по времени (чанки) и
    эффективную обработку временных рядов средствами стандартного SQL;
  * глубина хранения регулируется штатными политиками ретенции
    (add_retention_policy): сырые измерения хранятся 30 суток, часовые
    агрегаты и предсказания — пять лет;
  * для снижения объёма хранения применяется колоночное сжатие чанков
    (add_compression_policy) с сегментацией по идентификатору оборудования.

При недоступном TimescaleDB перечисленные выше шаги — no-op: логическая
схема таблиц не меняется, гипертаблицы и политики просто не создаются.
Ручного вызова ретенции приложение не делает: удаление устаревших данных —
дело политик TimescaleDB.

Метки времени во всех методах возвращаются строками ISO 8601 в UTC
(+00:00) — так же, как у резервного бэкенда SQLite (контракт хранилища
проверяется tests/storage_contract_test.py на обоих adapters).
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .config import settings
from .schema import (
    EquipmentRecord,
    HourlyAggregateRecord,
    PredictionRecord,
    TelemetryMeasurement,
)

log = logging.getLogger(__name__)

_DEFAULT_SEVERITY = "medium"


# Определение реляционной схемы. Расширение TimescaleDB (если доступно)
# подключается отдельным охраняемым шагом в _setup_timescaledb_extension —
# логическая схема таблиц от его наличия не зависит: на обычном PostgreSQL
# (например, managed-Postgres, где собственный образ
# timescale/timescaledb недоступен) telemetry_raw, telemetry_hourly и
# predictions остаются обычными таблицами.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS equipment (
    machine_id TEXT PRIMARY KEY,
    machine_type TEXT NOT NULL,
    operational_hours DOUBLE PRECISION NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS telemetry_raw (
    machine_id TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_raw_machine_ts
    ON telemetry_raw(machine_id, ts DESC);

CREATE TABLE IF NOT EXISTS telemetry_hourly (
    machine_id TEXT NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    features JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_hourly_machine_ts
    ON telemetry_hourly(machine_id, window_end DESC);

CREATE TABLE IF NOT EXISTS predictions (
    machine_id TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    failure_probability DOUBLE PRECISION NOT NULL,
    failure_label INTEGER NOT NULL,
    remaining_useful_life_days DOUBLE PRECISION NOT NULL,
    threshold DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_machine_ts
    ON predictions(machine_id, ts DESC);

CREATE TABLE IF NOT EXISTS alert_thresholds (
    machine_type TEXT PRIMARY KEY,
    threshold DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    machine_id TEXT NOT NULL REFERENCES equipment(machine_id),
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    opened_probability DOUBLE PRECISION NOT NULL,
    opened_rul_days DOUBLE PRECISION NOT NULL,
    peak_probability DOUBLE PRECISION NOT NULL,
    min_rul_days DOUBLE PRECISION NOT NULL,
    threshold DOUBLE PRECISION NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    peak_severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_incidents_machine
    ON incidents_log(machine_id, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_status
    ON incidents_log(status);

CREATE TABLE IF NOT EXISTS alerts_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id BIGINT REFERENCES incidents_log(id),
    machine_id TEXT NOT NULL REFERENCES equipment(machine_id),
    sent_at TIMESTAMPTZ NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    channel TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    group_key TEXT,
    grouped_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_machine_ts
    ON alerts_log(machine_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_group
    ON alerts_log(group_key, sent_at DESC);

-- Состав сводного оповещения: по записи на агрегат группы со своим
-- инцидентом и временем события (от него считается окно подавления).
CREATE TABLE IF NOT EXISTS alert_members (
    alert_id BIGINT NOT NULL REFERENCES alerts_log(id),
    machine_id TEXT NOT NULL REFERENCES equipment(machine_id),
    incident_id BIGINT REFERENCES incidents_log(id),
    event_time TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (alert_id, machine_id)
);
CREATE INDEX IF NOT EXISTS idx_alert_members_machine
    ON alert_members(machine_id, event_time DESC);

CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    email_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    sms_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    push_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    failure_threshold DOUBLE PRECISION,
    updated_at TIMESTAMPTZ
);
"""


def _iso(value) -> Optional[str]:
    """Приводит метку времени к строке ISO 8601 в UTC (+00:00).

    PostgreSQL отдаёт timestamptz в часовом поясе сессии; контракт
    хранилища требует UTC независимо от настроек сервера.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _clean_features(features: dict) -> dict:
    """NaN → None: в JSON NaN недопустим."""
    return {k: (None if v != v else v) for k, v in features.items()}


class PostgresDatabase:
    """Менеджер подключения к PostgreSQL/TimescaleDB (производственный бэкенд)."""

    backend = "postgres"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._lock = threading.RLock()
        # Доступность расширения TimescaleDB определяется отдельным
        # охраняемым шагом _setup_timescaledb_extension() при инициализации
        # схемы. По умолчанию считаем расширение недоступным — на обычном
        # PostgreSQL это единственно верное начальное значение.
        self.timescaledb_available = False
        self._conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        self._initialize_schema()

    # ------------------------------------------------------------ #
    # Инициализация схемы, гипертаблиц и политик
    # ------------------------------------------------------------ #
    def _initialize_schema(self) -> None:
        with self._lock:
            self._setup_timescaledb_extension()
            with self._conn.cursor() as cur:
                cur.execute(SCHEMA_DDL)
            self._conn.commit()
            self._migrate()
            self._setup_hypertables()
            self._setup_policies()

    def _setup_timescaledb_extension(self) -> None:
        """Охраняемый шаг: пытается подключить расширение TimescaleDB.

        На управляемом PostgreSQL без TimescaleDB (например, managed-PostgreSQL)
        команда CREATE EXTENSION завершится ошибкой доступа/отсутствия
        расширения в системном каталоге — это ожидаемая ситуация, а не
        авария. В этом случае схема инициализации продолжает работу на
        обычных таблицах PostgreSQL, поэтому трейсбек в лог не пишем —
        только короткое предупреждение.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            self._conn.commit()
            self.timescaledb_available = True
        except psycopg.Error as exc:
            self._conn.rollback()
            self.timescaledb_available = False
            log.warning(
                "Расширение TimescaleDB недоступно — схема на обычных "
                "таблицах PostgreSQL (%s)", exc
            )

    def _migrate(self) -> None:
        """Добавляет недостающие столбцы и корректирует ограничения старых таблиц."""
        # Шаг 1: добавить недостающие столбцы.
        migrations = [
            ("equipment", "category", "TEXT NOT NULL DEFAULT ''"),
            ("equipment", "operational_hours", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ]
        for table, column, decl in migrations:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = %s",
                        (table, column),
                    )
                    if cur.fetchone() is None:
                        cur.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                        )
                        self._conn.commit()
                        log.info("Миграция PG: добавлен столбец %s.%s", table, column)
            except Exception as exc:
                self._conn.rollback()
                log.warning("Ошибка миграции PG %s.%s: %s", table, column, exc)

        # Шаг 2: у устаревшего столбца installation_year задать DEFAULT,
        # чтобы INSERT без этого поля не нарушал ограничение NOT NULL.
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'equipment' AND column_name = 'installation_year'"
                )
                row = cur.fetchone()
                if row is not None and row["column_default"] is None:
                    cur.execute(
                        "ALTER TABLE equipment "
                        "ALTER COLUMN installation_year SET DEFAULT 0"
                    )
                    self._conn.commit()
                    log.info("Миграция PG: установлен DEFAULT 0 для equipment.installation_year")
        except Exception as exc:
            self._conn.rollback()
            log.warning("Ошибка миграции PG installation_year default: %s", exc)

    def _setup_hypertables(self) -> None:
        """Преобразует таблицы временных рядов в гипертаблицы TimescaleDB."""
        if not self.timescaledb_available:
            log.info(
                "Расширение TimescaleDB недоступно — гипертаблицы не "
                "создаются, telemetry_raw/telemetry_hourly/predictions "
                "остаются обычными таблицами PostgreSQL"
            )
            return
        statements = [
            "SELECT create_hypertable('telemetry_raw', 'ts', "
            "if_not_exists => TRUE, migrate_data => TRUE)",
            "SELECT create_hypertable('telemetry_hourly', 'window_end', "
            "if_not_exists => TRUE, migrate_data => TRUE)",
            "SELECT create_hypertable('predictions', 'ts', "
            "if_not_exists => TRUE, migrate_data => TRUE)",
        ]
        for sql in statements:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql)
                self._conn.commit()
            except psycopg.Error as exc:
                self._conn.rollback()
                log.warning("Гипертаблица не создана (%s): %s",
                            sql.split("'")[1], exc)

    def _setup_policies(self) -> None:
        """Регистрирует политики сжатия и ретенции TimescaleDB."""
        if not self.timescaledb_available:
            log.info(
                "Расширение TimescaleDB недоступно — политики сжатия и "
                "ретенции не регистрируются"
            )
            return
        raw_ret = settings.PG_RAW_RETENTION_DAYS
        agg_ret = settings.PG_AGG_RETENTION_DAYS
        raw_cmp = settings.PG_RAW_COMPRESS_AFTER_DAYS
        agg_cmp = settings.PG_AGG_COMPRESS_AFTER_DAYS
        statements = [
            # Включение колоночного сжатия с сегментацией по оборудованию.
            "ALTER TABLE telemetry_raw SET (timescaledb.compress, "
            "timescaledb.compress_segmentby = 'machine_id', "
            "timescaledb.compress_orderby = 'ts DESC')",
            "ALTER TABLE telemetry_hourly SET (timescaledb.compress, "
            "timescaledb.compress_segmentby = 'machine_id', "
            "timescaledb.compress_orderby = 'window_end DESC')",
            "ALTER TABLE predictions SET (timescaledb.compress, "
            "timescaledb.compress_segmentby = 'machine_id', "
            "timescaledb.compress_orderby = 'ts DESC')",
            # Политики сжатия (по возрасту чанка).
            f"SELECT add_compression_policy('telemetry_raw', "
            f"INTERVAL '{raw_cmp} days', if_not_exists => TRUE)",
            f"SELECT add_compression_policy('telemetry_hourly', "
            f"INTERVAL '{agg_cmp} days', if_not_exists => TRUE)",
            f"SELECT add_compression_policy('predictions', "
            f"INTERVAL '{agg_cmp} days', if_not_exists => TRUE)",
            # Политики ретенции (удаление устаревших чанков).
            f"SELECT add_retention_policy('telemetry_raw', "
            f"INTERVAL '{raw_ret} days', if_not_exists => TRUE)",
            f"SELECT add_retention_policy('telemetry_hourly', "
            f"INTERVAL '{agg_ret} days', if_not_exists => TRUE)",
            f"SELECT add_retention_policy('predictions', "
            f"INTERVAL '{agg_ret} days', if_not_exists => TRUE)",
        ]
        for sql in statements:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql)
                self._conn.commit()
            except psycopg.Error as exc:
                self._conn.rollback()
                log.warning("Политика TimescaleDB не применена: %s", exc)

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------ #
    # Equipment
    # ------------------------------------------------------------ #
    def upsert_equipment(self, record: EquipmentRecord) -> None:
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO equipment(machine_id, machine_type, operational_hours, category) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT(machine_id) DO UPDATE SET "
                    "machine_type = EXCLUDED.machine_type, "
                    "operational_hours = EXCLUDED.operational_hours, "
                    "category = CASE WHEN EXCLUDED.category != '' "
                    "           THEN EXCLUDED.category ELSE equipment.category END",
                    (record.machine_id, record.machine_type,
                     record.operational_hours, record.category),
                )

    def list_equipment(self) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT machine_id, machine_type, operational_hours, category "
                "FROM equipment ORDER BY machine_id"
            )
            return cur.fetchall()

    def has_predictions(self) -> bool:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM predictions")
            return cur.fetchone()["n"] > 0

    def get_sensor_averages(self, machine_id: str, days: int = 7) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    AVG((payload->>'temperature_c')::DOUBLE PRECISION)       AS temperature_c,
                    AVG((payload->>'vibration_mms')::DOUBLE PRECISION)        AS vibration_mms,
                    AVG((payload->>'sound_db')::DOUBLE PRECISION)             AS sound_db,
                    AVG((payload->>'oil_level_pct')::DOUBLE PRECISION)        AS oil_level_pct,
                    AVG((payload->>'coolant_level_pct')::DOUBLE PRECISION)    AS coolant_level_pct,
                    AVG((payload->>'power_consumption_kw')::DOUBLE PRECISION) AS power_consumption_kw
                FROM telemetry_raw
                WHERE machine_id = %s AND ts >= %s
                """,
                (machine_id, cutoff),
            )
            row = cur.fetchone()
            if row is None:
                return {}
            return {k: (round(v, 2) if v is not None else None) for k, v in row.items()}

    def get_alerts_count(self) -> int:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM alerts_log WHERE status='sent'")
            return cur.fetchone()["n"]

    def all_machines_overview(self) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT machine_id, MAX(ts) AS ts
                    FROM predictions GROUP BY machine_id
                ),
                latest_tel AS (
                    SELECT machine_id, MAX(ts) AS ts
                    FROM telemetry_raw GROUP BY machine_id
                )
                SELECT e.machine_id, e.machine_type, e.operational_hours, e.category,
                       p.ts AS timestamp, p.failure_probability, p.failure_label,
                       p.remaining_useful_life_days, p.threshold,
                       (t.payload->>'temperature_c')::DOUBLE PRECISION       AS temperature_c,
                       (t.payload->>'vibration_mms')::DOUBLE PRECISION        AS vibration_mms,
                       (t.payload->>'sound_db')::DOUBLE PRECISION             AS sound_db,
                       (t.payload->>'oil_level_pct')::DOUBLE PRECISION        AS oil_level_pct,
                       (t.payload->>'coolant_level_pct')::DOUBLE PRECISION    AS coolant_level_pct,
                       (t.payload->>'power_consumption_kw')::DOUBLE PRECISION AS power_consumption_kw
                FROM equipment e
                LEFT JOIN latest ON latest.machine_id = e.machine_id
                LEFT JOIN predictions p
                    ON p.machine_id = latest.machine_id AND p.ts = latest.ts
                LEFT JOIN latest_tel ON latest_tel.machine_id = e.machine_id
                LEFT JOIN telemetry_raw t
                    ON t.machine_id = latest_tel.machine_id AND t.ts = latest_tel.ts
                ORDER BY COALESCE(p.failure_probability, -1) DESC, e.machine_id
                """
            )
            rows = [self._fix_ts(dict(r)) for r in cur.fetchall()]
        return rows

    # ------------------------------------------------------------ #
    # Настройки оповещений (профиль инспектора БППР)
    # ------------------------------------------------------------ #
    def get_notification_settings(self) -> Optional[dict]:
        """Сохранённые настройки оповещений или None, если их ещё не сохраняли.

        Значения по умолчанию adapter не знает — их подставляет
        app.notification_settings.load_notification_settings."""
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT email_enabled, sms_enabled, push_enabled, email, phone, "
                "failure_threshold, updated_at FROM notification_settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None:
            return None
        row["email_enabled"] = bool(row["email_enabled"])
        row["sms_enabled"] = bool(row["sms_enabled"])
        row["push_enabled"] = bool(row["push_enabled"])
        row["updated_at"] = _iso(row.get("updated_at"))
        return row

    def save_notification_settings(self, *, email_enabled: bool, sms_enabled: bool,
                                   push_enabled: bool, email: str, phone: str,
                                   failure_threshold: float) -> dict:
        now = datetime.now(timezone.utc)
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notification_settings(id, email_enabled, sms_enabled, "
                "push_enabled, email, phone, failure_threshold, updated_at) "
                "VALUES (1, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT(id) DO UPDATE SET "
                "email_enabled=EXCLUDED.email_enabled, sms_enabled=EXCLUDED.sms_enabled, "
                "push_enabled=EXCLUDED.push_enabled, email=EXCLUDED.email, "
                "phone=EXCLUDED.phone, failure_threshold=EXCLUDED.failure_threshold, "
                "updated_at=EXCLUDED.updated_at",
                (bool(email_enabled), bool(sms_enabled), bool(push_enabled),
                 email, phone, failure_threshold, now),
            )
        return self.get_notification_settings()

    # ------------------------------------------------------------ #
    # Пакетная запись истории (одна транзакция на всю партию)
    # ------------------------------------------------------------ #
    def insert_history(
        self,
        measurements: list[TelemetryMeasurement],
        predictions: list[PredictionRecord],
        hourly_aggregates: list[HourlyAggregateRecord],
    ) -> None:
        """Записывает историю типизированными записями одной транзакцией.

        Формат строк совпадает с поштучными insert_raw_measurement,
        insert_prediction и insert_hourly_aggregate живого потока.
        Идемпотентна по (агрегат, время): точка, которая уже есть в базе,
        повторно не записывается.
        """
        with self._transaction() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO telemetry_raw(machine_id, ts, payload) "
                    "SELECT %s, %s::timestamptz, %s::jsonb WHERE NOT EXISTS ("
                    "SELECT 1 FROM telemetry_raw WHERE machine_id = %s AND ts = %s::timestamptz)",
                    [
                        (m.machine_id, m.timestamp, Json(m.model_dump(mode="json")),
                         m.machine_id, m.timestamp)
                        for m in measurements
                    ],
                )
                cur.executemany(
                    "INSERT INTO predictions"
                    "(machine_id, ts, failure_probability, "
                    "failure_label, remaining_useful_life_days, threshold) "
                    "SELECT %s, %s::timestamptz, %s, %s, %s, %s WHERE NOT EXISTS ("
                    "SELECT 1 FROM predictions WHERE machine_id = %s AND ts = %s::timestamptz)",
                    [
                        (p.machine_id, p.timestamp, p.failure_probability,
                         p.failure_label, p.remaining_useful_life_days, p.threshold,
                         p.machine_id, p.timestamp)
                        for p in predictions
                    ],
                )
                cur.executemany(
                    "INSERT INTO telemetry_hourly(machine_id, window_end, features) "
                    "SELECT %s, %s::timestamptz, %s::jsonb WHERE NOT EXISTS ("
                    "SELECT 1 FROM telemetry_hourly "
                    "WHERE machine_id = %s AND window_end = %s::timestamptz)",
                    [
                        (h.machine_id, h.window_end, Json(_clean_features(h.features)),
                         h.machine_id, h.window_end)
                        for h in hourly_aggregates
                    ],
                )

    # ------------------------------------------------------------ #
    # Raw telemetry
    # ------------------------------------------------------------ #
    def insert_raw_measurement(self, measurement: TelemetryMeasurement) -> None:
        payload = measurement.model_dump(mode="json")
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry_raw(machine_id, ts, payload) "
                "VALUES (%s, %s, %s)",
                (measurement.machine_id, measurement.timestamp, Json(payload)),
            )

    def latest_raw_measurements(self, machine_id: str, limit: int = 50) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT ts, payload FROM telemetry_raw "
                "WHERE machine_id = %s ORDER BY ts DESC LIMIT %s",
                (machine_id, limit),
            )
            rows = []
            for row in cur.fetchall():
                payload = row["payload"] or {}
                # Метка строки в UTC перекрывает метку из payload (исходное смещение).
                rows.append({**payload, "timestamp": _iso(row["ts"])})
            return rows

    # ------------------------------------------------------------ #
    # Hourly aggregates
    # ------------------------------------------------------------ #
    def insert_hourly_aggregate(self, machine_id: str, window_end: datetime,
                                features: dict) -> None:
        clean = _clean_features(features)
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry_hourly(machine_id, window_end, features) "
                "VALUES (%s, %s, %s)",
                (machine_id, window_end, Json(clean)),
            )

    # ------------------------------------------------------------ #
    # Predictions
    # ------------------------------------------------------------ #
    def insert_prediction(self, record: PredictionRecord) -> None:
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO predictions(machine_id, ts, failure_probability, "
                "failure_label, remaining_useful_life_days, threshold) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (record.machine_id, record.timestamp, record.failure_probability,
                 record.failure_label, record.remaining_useful_life_days,
                 record.threshold),
            )

    def latest_predictions(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT p.machine_id, p.ts AS timestamp, p.failure_probability, "
                "p.failure_label, p.remaining_useful_life_days, p.threshold, "
                "e.machine_type FROM predictions p "
                "LEFT JOIN equipment e ON e.machine_id = p.machine_id "
                "ORDER BY p.ts DESC LIMIT %s",
                (limit,),
            )
            return [self._fix_ts(r) for r in cur.fetchall()]

    def predictions_history(self, machine_id: str, limit: int = 200) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT ts AS timestamp, failure_probability, failure_label, "
                "remaining_useful_life_days, threshold FROM predictions "
                "WHERE machine_id = %s ORDER BY ts DESC LIMIT %s",
                (machine_id, limit),
            )
            return [self._fix_ts(r) for r in cur.fetchall()]

    @staticmethod
    def _fix_ts(row: dict) -> dict:
        if "timestamp" in row:
            row["timestamp"] = _iso(row["timestamp"])
        return row

    # ------------------------------------------------------------ #
    # Incidents log
    # ------------------------------------------------------------ #
    def get_open_incident(self, machine_id: str) -> Optional[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM incidents_log WHERE machine_id = %s AND status='open' "
                "ORDER BY opened_at DESC LIMIT 1",
                (machine_id,),
            )
            row = cur.fetchone()
            if row:
                row["opened_at"] = _iso(row.get("opened_at"))
                row["closed_at"] = _iso(row.get("closed_at"))
            return row

    def open_incident(self, machine_id: str, opened_at: datetime,
                      probability: float, rul_days: float, threshold: float,
                      severity: str = _DEFAULT_SEVERITY) -> int:
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incidents_log(machine_id, opened_at, opened_probability, "
                "opened_rul_days, peak_probability, min_rul_days, threshold, "
                "severity, peak_severity, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open') RETURNING id",
                (machine_id, opened_at, probability, rul_days, probability,
                 rul_days, threshold, severity, severity),
            )
            return int(cur.fetchone()["id"])

    def update_open_incident(self, incident_id: int, probability: float,
                             rul_days: float,
                             peak_severity: str = _DEFAULT_SEVERITY) -> None:
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE incidents_log SET peak_probability = GREATEST(peak_probability, %s), "
                "min_rul_days = LEAST(min_rul_days, %s), peak_severity = %s WHERE id = %s",
                (probability, rul_days, peak_severity, incident_id),
            )

    def close_incident(self, incident_id: int, closed_at: datetime) -> None:
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE incidents_log SET status='closed', closed_at=%s WHERE id=%s",
                (closed_at, incident_id),
            )

    def list_incidents(self, status: Optional[str] = None,
                       machine_type: Optional[str] = None,
                       limit: int = 200) -> list[dict]:
        query = (
            "SELECT i.id, i.machine_id, e.machine_type, i.opened_at, "
            "i.closed_at, i.opened_probability, i.peak_probability, "
            "i.opened_rul_days, i.min_rul_days, i.threshold, "
            "i.severity, i.peak_severity, i.status "
            "FROM incidents_log i LEFT JOIN equipment e "
            "ON e.machine_id = i.machine_id WHERE 1=1"
        )
        params: list = []
        if status:
            query += " AND i.status = %s"; params.append(status)
        if machine_type:
            query += " AND e.machine_type = %s"; params.append(machine_type)
        query += " ORDER BY i.opened_at DESC LIMIT %s"
        params.append(limit)
        with self._lock, self._conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        for r in rows:
            r["opened_at"] = _iso(r.get("opened_at"))
            r["closed_at"] = _iso(r.get("closed_at"))
        return rows

    def incidents_summary(self) -> dict:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM incidents_log GROUP BY status")
            by = {row["status"]: row["n"] for row in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS n FROM incidents_log")
            total = cur.fetchone()["n"]
            cur.execute("SELECT peak_severity, COUNT(*) AS n FROM incidents_log "
                        "WHERE status='open' GROUP BY peak_severity")
            by_sev = {row["peak_severity"]: row["n"] for row in cur.fetchall()}
        return {
            "total": total,
            "open": by.get("open", 0),
            "closed": by.get("closed", 0),
            "open_by_severity": by_sev,
        }

    # ------------------------------------------------------------ #
    # Alerts log
    # ------------------------------------------------------------ #
    def last_alert_for_machine(self, machine_id: str) -> Optional[dict]:
        """Последнее успешное оповещение по агрегату — одиночное или в
        составе сводного. У сводного sent_at и incident_id берутся из
        записи состава этого агрегата."""
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT a.*, m.event_time AS member_time, "
                "m.incident_id AS member_incident FROM alerts_log a "
                "JOIN alert_members m ON m.alert_id = a.id "
                "WHERE m.machine_id = %s AND a.status = 'sent' "
                "ORDER BY m.event_time DESC LIMIT 1",
                (machine_id,),
            )
            grouped = cur.fetchone()
            cur.execute(
                "SELECT * FROM alerts_log WHERE machine_id = %s AND status='sent' "
                "AND NOT EXISTS (SELECT 1 FROM alert_members m "
                "WHERE m.alert_id = alerts_log.id) "
                "ORDER BY sent_at DESC LIMIT 1",
                (machine_id,),
            )
            row = cur.fetchone()
        if grouped is not None and (row is None
                                    or grouped["member_time"] > row["sent_at"]):
            row = grouped
            row["sent_at"] = row.pop("member_time")
            row["incident_id"] = row.pop("member_incident")
        if row:
            row["sent_at"] = _iso(row.get("sent_at"))
        return row

    def _alert_members(self, cur, alert_ids: list[int]) -> dict[int, list[dict]]:
        if not alert_ids:
            return {}
        cur.execute(
            "SELECT alert_id, machine_id, incident_id FROM alert_members "
            "WHERE alert_id = ANY(%s) ORDER BY machine_id",
            (alert_ids,),
        )
        out: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            out.setdefault(r["alert_id"], []).append(
                {"machine_id": r["machine_id"], "incident_id": r["incident_id"]})
        return out

    def get_alert(self, alert_id: int) -> Optional[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, incident_id, machine_id, sent_at, recipient, "
                "subject, body, channel, severity, group_key, grouped_count, "
                "status, error FROM alerts_log WHERE id = %s",
                (alert_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            row["sent_at"] = _iso(row.get("sent_at"))
            row["members"] = self._alert_members(cur, [alert_id]).get(alert_id, [])
            return row

    def insert_alert(self, incident_id: Optional[int], machine_id: str,
                     sent_at: datetime, recipient: str, subject: str,
                     body: str, channel: str, status: str,
                     severity: str = _DEFAULT_SEVERITY,
                     group_key: Optional[str] = None,
                     grouped_count: int = 1,
                     error: Optional[str] = None,
                     members: Optional[list[dict]] = None) -> int:
        """Записывает попытку доставки. members — состав сводного
        оповещения: [{machine_id, incident_id, event_time}], пишется в
        той же транзакции."""
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts_log(incident_id, machine_id, sent_at, recipient, "
                "subject, body, channel, severity, group_key, grouped_count, "
                "status, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (incident_id, machine_id, sent_at, recipient, subject, body,
                 channel, severity, group_key, grouped_count, status, error),
            )
            alert_id = int(cur.fetchone()["id"])
            for m in members or ():
                cur.execute(
                    "INSERT INTO alert_members(alert_id, machine_id, incident_id, "
                    "event_time) VALUES (%s, %s, %s, %s)",
                    (alert_id, m["machine_id"], m["incident_id"], m["event_time"]),
                )
            return alert_id

    def list_alerts(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, incident_id, machine_id, sent_at, recipient, "
                "subject, channel, severity, group_key, grouped_count, "
                "status, error FROM alerts_log ORDER BY sent_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
            members = self._alert_members(cur, [r["id"] for r in rows])
        for r in rows:
            r["sent_at"] = _iso(r.get("sent_at"))
            r["members"] = members.get(r["id"], [])
        return rows

    def close(self) -> None:
        with self._lock:
            self._conn.close()
