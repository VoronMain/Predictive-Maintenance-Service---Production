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
  * повторная инициализация на той же БД идемпотентна;
  * таблицы временных рядов telemetry_raw / telemetry_hourly / predictions
    созданы как обычные таблицы, а не гипертаблицы (нет схемы
    _timescaledb_catalog, relkind = 'r').

Здесь только то, что специфично для PostgreSQL. Чтение и запись данных
(в том числе на базе без TimescaleDB) проверяются контрактным набором
tests/storage_contract_test.py — он идёт на обычном PostgreSQL именно так.

Тест требует Docker. При его отсутствии (нет пакета testcontainers, не
запущен демон Docker) фикстура pg_dsn (tests/conftest.py) пропускает тесты
с причиной; в CI SPA_REQUIRE_PG_TESTS=1 превращает пропуск в ошибку.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def db(pg_dsn):
    """Свежий PostgresDatabase на «голом» PostgreSQL."""
    from app.pg_database import PostgresDatabase

    instance = PostgresDatabase(pg_dsn)
    try:
        yield instance
    finally:
        instance.close()


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
