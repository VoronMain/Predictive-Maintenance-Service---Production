# -*- coding: utf-8 -*-
"""database.py - подсистема хранения данных СПА.

Подсистема хранения реализована в виде взаимозаменяемых бэкендов с
единым программным интерфейсом:

  * SQLiteDatabase    — резервный бэкенд на встроенной СУБД SQLite,
    применяемый при автономном запуске демонстрационного стенда и при
    выполнении автоматизированных тестов без поднятия серверной СУБД;
  * PostgresDatabase  — производственный бэкенд на базе PostgreSQL с
    расширением TimescaleDB (модуль app/pg_database.py); подключается
    фабрикой create_database() при SPA_DB_BACKEND=postgres.

Выбор бэкенда осуществляется фабрикой create_database() на основании
конфигурации. Прикладной код подсистем (конвейер, детектор инцидентов,
сервис оповещений) не зависит от конкретной реализации хранения и
взаимодействует с ней исключительно через общий интерфейс.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import settings
from .schema import EquipmentRecord, PredictionRecord, TelemetryMeasurement

log = logging.getLogger(__name__)

_DEFAULT_SEVERITY = "medium"


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS equipment (
    machine_id TEXT PRIMARY KEY,
    machine_type TEXT NOT NULL,
    operational_hours REAL NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS telemetry_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (machine_id) REFERENCES equipment(machine_id)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_raw_machine_ts
    ON telemetry_raw(machine_id, timestamp);

CREATE TABLE IF NOT EXISTS telemetry_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    window_end TEXT NOT NULL,
    features TEXT NOT NULL,
    FOREIGN KEY (machine_id) REFERENCES equipment(machine_id)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_hourly_machine_ts
    ON telemetry_hourly(machine_id, window_end);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    failure_probability REAL NOT NULL,
    failure_label INTEGER NOT NULL,
    remaining_useful_life_days REAL NOT NULL,
    threshold REAL NOT NULL,
    FOREIGN KEY (machine_id) REFERENCES equipment(machine_id)
);
CREATE INDEX IF NOT EXISTS idx_predictions_machine_ts
    ON predictions(machine_id, timestamp);

CREATE TABLE IF NOT EXISTS alert_thresholds (
    machine_type TEXT PRIMARY KEY,
    threshold REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    opened_probability REAL NOT NULL,
    opened_rul_days REAL NOT NULL,
    peak_probability REAL NOT NULL,
    min_rul_days REAL NOT NULL,
    threshold REAL NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    peak_severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    FOREIGN KEY (machine_id) REFERENCES equipment(machine_id)
);
CREATE INDEX IF NOT EXISTS idx_incidents_machine
    ON incidents_log(machine_id, opened_at);
CREATE INDEX IF NOT EXISTS idx_incidents_status
    ON incidents_log(status);

CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER,
    machine_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    channel TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    group_key TEXT,
    grouped_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents_log(id),
    FOREIGN KEY (machine_id) REFERENCES equipment(machine_id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_machine_ts
    ON alerts_log(machine_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_group
    ON alerts_log(group_key, sent_at);

CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    email_enabled INTEGER NOT NULL DEFAULT 1,
    sms_enabled INTEGER NOT NULL DEFAULT 0,
    push_enabled INTEGER NOT NULL DEFAULT 0,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    failure_threshold REAL,
    updated_at TEXT
);
"""


_MIGRATIONS = (
    ("incidents_log", "severity", "TEXT NOT NULL DEFAULT 'medium'"),
    ("incidents_log", "peak_severity", "TEXT NOT NULL DEFAULT 'medium'"),
    ("alerts_log", "severity", "TEXT NOT NULL DEFAULT 'medium'"),
    ("alerts_log", "group_key", "TEXT"),
    ("alerts_log", "grouped_count", "INTEGER NOT NULL DEFAULT 1"),
    ("equipment", "category", "TEXT NOT NULL DEFAULT ''"),
    ("equipment", "operational_hours", "REAL NOT NULL DEFAULT 0"),
)


class SQLiteDatabase:
    """Менеджер подключения к базе данных СПА (резервный бэкенд SQLite)."""

    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_DDL)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Добавляет недостающие столбцы в ранее созданные базы данных."""
        for table, column, decl in _MIGRATIONS:
            cur = self._conn.execute(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in cur.fetchall()}
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
                log.info("Миграция: добавлен столбец %s.%s", table, column)

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ===== Equipment =====
    def upsert_equipment(self, record: EquipmentRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO equipment(machine_id, machine_type, operational_hours, category) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(machine_id) DO UPDATE SET "
                "machine_type=excluded.machine_type, "
                "operational_hours=excluded.operational_hours, "
                "category=CASE WHEN excluded.category != '' "
                "         THEN excluded.category ELSE equipment.category END",
                (record.machine_id, record.machine_type,
                 record.operational_hours, record.category),
            )

    def list_equipment(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT machine_id, machine_type, operational_hours, category "
                "FROM equipment ORDER BY machine_id"
            )
            return [dict(row) for row in cur.fetchall()]

    def has_predictions(self) -> bool:
        """Возвращает True, если в базе данных есть хотя бы одно предсказание."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM predictions")
            return cur.fetchone()["n"] > 0

    def get_sensor_averages(self, machine_id: str, days: int = 7) -> dict:
        """Средние значения шести сенсоров за последние N дней из сырых измерений."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    AVG(json_extract(payload, '$.temperature_c'))      AS temperature_c,
                    AVG(json_extract(payload, '$.vibration_mms'))       AS vibration_mms,
                    AVG(json_extract(payload, '$.sound_db'))            AS sound_db,
                    AVG(json_extract(payload, '$.oil_level_pct'))       AS oil_level_pct,
                    AVG(json_extract(payload, '$.coolant_level_pct'))   AS coolant_level_pct,
                    AVG(json_extract(payload, '$.power_consumption_kw')) AS power_consumption_kw
                FROM telemetry_raw
                WHERE machine_id = ? AND timestamp >= ?
                """,
                (machine_id, cutoff),
            )
            row = cur.fetchone()
            if row is None:
                return {}
            return {k: (round(v, 2) if v is not None else None)
                    for k, v in dict(row).items()}

    def get_alerts_count(self) -> int:
        """Количество успешно отправленных оповещений."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS n FROM alerts_log WHERE status='sent'"
            )
            return cur.fetchone()["n"]

    def all_machines_overview(self) -> list[dict]:
        """Все агрегаты с последним предсказанием и последними значениями
        датчиков (NULL у машин без предсказаний/телеметрии)."""
        with self._lock:
            cur = self._conn.execute(
                """
                WITH latest AS (
                    SELECT machine_id, MAX(timestamp) AS ts
                    FROM predictions GROUP BY machine_id
                ),
                latest_tel AS (
                    SELECT machine_id, MAX(timestamp) AS ts
                    FROM telemetry_raw GROUP BY machine_id
                )
                SELECT e.machine_id, e.machine_type, e.operational_hours, e.category,
                       p.timestamp, p.failure_probability, p.failure_label,
                       p.remaining_useful_life_days, p.threshold,
                       json_extract(t.payload, '$.temperature_c')       AS temperature_c,
                       json_extract(t.payload, '$.vibration_mms')        AS vibration_mms,
                       json_extract(t.payload, '$.sound_db')             AS sound_db,
                       json_extract(t.payload, '$.oil_level_pct')        AS oil_level_pct,
                       json_extract(t.payload, '$.coolant_level_pct')    AS coolant_level_pct,
                       json_extract(t.payload, '$.power_consumption_kw') AS power_consumption_kw
                FROM equipment e
                LEFT JOIN latest ON latest.machine_id = e.machine_id
                LEFT JOIN predictions p
                    ON p.machine_id = latest.machine_id AND p.timestamp = latest.ts
                LEFT JOIN latest_tel ON latest_tel.machine_id = e.machine_id
                LEFT JOIN telemetry_raw t
                    ON t.machine_id = latest_tel.machine_id AND t.timestamp = latest_tel.ts
                ORDER BY COALESCE(p.failure_probability, -1) DESC, e.machine_id
                """
            )
            return [dict(row) for row in cur.fetchall()]

    # ===== Настройки оповещений (профиль инспектора БППР) =====
    def get_notification_settings(self) -> dict:
        """Возвращает текущие настройки оповещений. При отсутствии записи
        формирует значения по умолчанию из конфигурации приложения."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT email_enabled, sms_enabled, push_enabled, email, phone, "
                "failure_threshold, updated_at FROM notification_settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None:
            return {
                "email_enabled": True,
                "sms_enabled": False,
                "push_enabled": False,
                "email": settings.SMTP_TO,
                "phone": "",
                "failure_threshold": settings.FAILURE_THRESHOLD,
                "updated_at": None,
            }
        d = dict(row)
        d["email_enabled"] = bool(d["email_enabled"])
        d["sms_enabled"] = bool(d["sms_enabled"])
        d["push_enabled"] = bool(d["push_enabled"])
        if d.get("failure_threshold") is None:
            d["failure_threshold"] = settings.FAILURE_THRESHOLD
        return d

    def save_notification_settings(self, *, email_enabled: bool, sms_enabled: bool,
                                   push_enabled: bool, email: str, phone: str,
                                   failure_threshold: float) -> dict:
        """Сохраняет настройки оповещений (единственная строка id=1)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO notification_settings(id, email_enabled, sms_enabled, "
                "push_enabled, email, phone, failure_threshold, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "email_enabled=excluded.email_enabled, sms_enabled=excluded.sms_enabled, "
                "push_enabled=excluded.push_enabled, email=excluded.email, "
                "phone=excluded.phone, failure_threshold=excluded.failure_threshold, "
                "updated_at=excluded.updated_at",
                (int(bool(email_enabled)), int(bool(sms_enabled)),
                 int(bool(push_enabled)), email, phone, failure_threshold, now),
            )
        return self.get_notification_settings()

    # ===== Пакетная вставка для засева (одна транзакция на всю партию) =====
    def bulk_insert_for_seed(
        self,
        telemetry_rows: list[tuple],
        prediction_rows: list[tuple],
    ) -> None:
        """Вставляет данные засева одной транзакцией.

        telemetry_rows: список кортежей (machine_id, timestamp, payload_json)
        prediction_rows: список кортежей
            (machine_id, timestamp, failure_probability,
             failure_label, remaining_useful_life_days, threshold)
        """
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO telemetry_raw(machine_id, timestamp, payload) "
                "VALUES (?, ?, ?)",
                telemetry_rows,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO predictions"
                "(machine_id, timestamp, failure_probability, "
                "failure_label, remaining_useful_life_days, threshold) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                prediction_rows,
            )

    # ===== Raw telemetry =====
    def insert_raw_measurement(self, measurement: TelemetryMeasurement) -> None:
        payload = measurement.model_dump(mode="json")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO telemetry_raw(machine_id, timestamp, payload) "
                "VALUES (?, ?, ?)",
                (
                    measurement.machine_id,
                    measurement.timestamp.isoformat(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def latest_raw_measurements(self, machine_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT timestamp, payload FROM telemetry_raw "
                "WHERE machine_id = ? ORDER BY timestamp DESC LIMIT ?",
                (machine_id, limit),
            )
            rows = []
            for row in cur.fetchall():
                rows.append({"timestamp": row["timestamp"], **json.loads(row["payload"])})
            return rows

    # ===== Hourly aggregates =====
    def insert_hourly_aggregate(self, machine_id: str, window_end: datetime, features: dict) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO telemetry_hourly(machine_id, window_end, features) "
                "VALUES (?, ?, ?)",
                (
                    machine_id,
                    window_end.isoformat(),
                    json.dumps(features, ensure_ascii=False, default=float),
                ),
            )

    # ===== Predictions =====
    def insert_prediction(self, record: PredictionRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO predictions(machine_id, timestamp, failure_probability, "
                "failure_label, remaining_useful_life_days, threshold) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.machine_id,
                    record.timestamp.isoformat(),
                    record.failure_probability,
                    record.failure_label,
                    record.remaining_useful_life_days,
                    record.threshold,
                ),
            )

    def latest_predictions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT p.machine_id, p.timestamp, p.failure_probability, "
                "p.failure_label, p.remaining_useful_life_days, p.threshold, "
                "e.machine_type FROM predictions p "
                "LEFT JOIN equipment e ON e.machine_id = p.machine_id "
                "ORDER BY p.timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def latest_prediction_per_machine(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT p.machine_id, p.timestamp, p.failure_probability, "
                "p.failure_label, p.remaining_useful_life_days, p.threshold, "
                "e.machine_type, e.operational_hours, e.category FROM predictions p "
                "JOIN (SELECT machine_id, MAX(timestamp) AS ts FROM predictions "
                "GROUP BY machine_id) last "
                "ON last.machine_id = p.machine_id AND last.ts = p.timestamp "
                "LEFT JOIN equipment e ON e.machine_id = p.machine_id "
                "ORDER BY p.failure_probability DESC"
            )
            return [dict(row) for row in cur.fetchall()]

    def predictions_history(self, machine_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT timestamp, failure_probability, failure_label, "
                "remaining_useful_life_days, threshold FROM predictions "
                "WHERE machine_id = ? ORDER BY timestamp DESC LIMIT ?",
                (machine_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    # ===== Retention =====
    def apply_retention_policy(self, raw_retention_days: int = 30,
                                aggregated_retention_days: int = 365 * 5) -> None:
        """Удаляет устаревшие записи (резервный бэкенд SQLite).

        В производственном бэкенде PostgreSQL/TimescaleDB ретенция
        реализована штатными политиками add_retention_policy.
        """
        now = datetime.utcnow()
        raw_cutoff = (now - timedelta(days=raw_retention_days)).isoformat()
        agg_cutoff = (now - timedelta(days=aggregated_retention_days)).isoformat()
        with self.transaction() as conn:
            conn.execute("DELETE FROM telemetry_raw WHERE timestamp < ?", (raw_cutoff,))
            conn.execute("DELETE FROM telemetry_hourly WHERE window_end < ?", (agg_cutoff,))
            conn.execute("DELETE FROM predictions WHERE timestamp < ?", (agg_cutoff,))

    # ===== Incidents log =====
    def get_open_incident(self, machine_id: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM incidents_log WHERE machine_id = ? AND status='open' "
                "ORDER BY opened_at DESC LIMIT 1",
                (machine_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def open_incident(self, machine_id: str, opened_at: datetime,
                      probability: float, rul_days: float, threshold: float,
                      severity: str = _DEFAULT_SEVERITY) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO incidents_log(machine_id, opened_at, opened_probability, "
                "opened_rul_days, peak_probability, min_rul_days, threshold, "
                "severity, peak_severity, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (machine_id, opened_at.isoformat(), probability, rul_days,
                 probability, rul_days, threshold, severity, severity),
            )
            return int(cur.lastrowid)

    def update_open_incident(self, incident_id: int, probability: float,
                             rul_days: float,
                             peak_severity: str = _DEFAULT_SEVERITY) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE incidents_log SET peak_probability=MAX(peak_probability, ?), "
                "min_rul_days=MIN(min_rul_days, ?), peak_severity=? WHERE id=?",
                (probability, rul_days, peak_severity, incident_id),
            )

    def close_incident(self, incident_id: int, closed_at: datetime) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE incidents_log SET status='closed', closed_at=? WHERE id=?",
                (closed_at.isoformat(), incident_id),
            )

    def list_incidents(self, status: Optional[str] = None,
                       machine_type: Optional[str] = None,
                       date_from: Optional[datetime] = None,
                       date_to: Optional[datetime] = None,
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
            query += " AND i.status = ?"; params.append(status)
        if machine_type:
            query += " AND e.machine_type = ?"; params.append(machine_type)
        if date_from:
            query += " AND i.opened_at >= ?"; params.append(date_from.isoformat())
        if date_to:
            query += " AND i.opened_at <= ?"; params.append(date_to.isoformat())
        query += " ORDER BY i.opened_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def incidents_summary(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM incidents_log GROUP BY status"
            )
            by = {row["status"]: row["n"] for row in cur.fetchall()}
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM incidents_log")
            total = cur.fetchone()["n"]
            cur = self._conn.execute(
                "SELECT peak_severity, COUNT(*) AS n FROM incidents_log "
                "WHERE status='open' GROUP BY peak_severity"
            )
            by_sev = {row["peak_severity"]: row["n"] for row in cur.fetchall()}
        return {
            "total": total,
            "open": by.get("open", 0),
            "closed": by.get("closed", 0),
            "open_by_severity": by_sev,
        }

    # ===== Alerts log =====
    def last_alert_for_machine(self, machine_id: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM alerts_log WHERE machine_id = ? AND status='sent' "
                "ORDER BY sent_at DESC LIMIT 1",
                (machine_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def insert_alert(self, incident_id: Optional[int], machine_id: str,
                     sent_at: datetime, recipient: str, subject: str,
                     body: str, channel: str, status: str,
                     severity: str = _DEFAULT_SEVERITY,
                     group_key: Optional[str] = None,
                     grouped_count: int = 1,
                     error: Optional[str] = None) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO alerts_log(incident_id, machine_id, sent_at, recipient, "
                "subject, body, channel, severity, group_key, grouped_count, "
                "status, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (incident_id, machine_id, sent_at.isoformat(), recipient,
                 subject, body, channel, severity, group_key, grouped_count,
                 status, error),
            )
            return int(cur.lastrowid)

    def get_alert(self, alert_id: int) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, incident_id, machine_id, sent_at, recipient, "
                "subject, body, channel, severity, group_key, grouped_count, "
                "status, error FROM alerts_log WHERE id = ?",
                (alert_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_alerts(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, incident_id, machine_id, sent_at, recipient, "
                "subject, channel, severity, group_key, grouped_count, "
                "status, error FROM alerts_log "
                "ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Псевдоним для обратной совместимости: исторически прикладной код и
# автоматизированные тесты обращаются к классу Database.
Database = SQLiteDatabase


def create_database():
    """Фабрика подсистемы хранения данных.

    Возвращает экземпляр бэкенда в соответствии с конфигурацией
    SPA_DB_BACKEND. Производственный бэкенд PostgreSQL/TimescaleDB
    импортируется лениво, что снимает зависимость от драйвера psycopg
    при автономном запуске на резервном бэкенде SQLite.
    """
    backend = settings.DB_BACKEND
    if backend == "postgres":
        from .pg_database import PostgresDatabase
        log.info("Подсистема хранения: PostgreSQL/TimescaleDB (%s:%s/%s)",
                 settings.PG_HOST, settings.PG_PORT, settings.PG_DB)
        return PostgresDatabase(settings.pg_dsn)
    if backend != "sqlite":
        log.warning("Неизвестный бэкенд %r, применён резервный SQLite", backend)
    log.info("Подсистема хранения: SQLite (%s)", settings.DB_PATH)
    return SQLiteDatabase(settings.DB_PATH)
