# -*- coding: utf-8 -*-
# Корень проекта уже добавлен в sys.path через pytest.ini (pythonpath = .)
"""Общие фикстуры тестов: эфемерный PostgreSQL в контейнере.

Фикстура pg_dsn поднимает один контейнер на тестовый модуль (а не на тест)
и мягко пропускает зависящие от неё тесты, если Docker или пакет
testcontainers недоступны. В CI выставлена SPA_REQUIRE_PG_TESTS=1: там
отсутствие Docker — это ошибка, а не пропуск, иначе зелёный прогон
ничего не доказывал бы про PostgreSQL.
"""
from __future__ import annotations

import os
import re

import pytest

# Обычный PostgreSQL — заведомо без расширения TimescaleDB.
PG_IMAGE = "postgres:16-alpine"


def _pg_unavailable(reason: str):
    if os.environ.get("SPA_REQUIRE_PG_TESTS") == "1":
        pytest.fail(f"SPA_REQUIRE_PG_TESTS=1, но PostgreSQL недоступен: {reason}")
    pytest.skip(f"PostgreSQL недоступен — тесты пропущены: {reason}")


def libpq_dsn(container) -> str:
    """URL от testcontainers → строка подключения в формате psycopg/libpq.

    get_connection_url() отдаёт SQLAlchemy-URL вида
    postgresql+psycopg2://user:pass@host:port/db; psycopg3 понимает
    postgresql://, поэтому суффикс драйвера убираем.
    """
    url = container.get_connection_url()
    return re.sub(r"^postgresql\+[a-z0-9]+://", "postgresql://", url)


@pytest.fixture(scope="module")
def pg_dsn():
    """Поднимает обычный PostgreSQL в контейнере и отдаёт DSN."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        _pg_unavailable(f"не установлен testcontainers ({exc})")
        return

    try:
        container = PostgresContainer(PG_IMAGE)
        container.start()
    except Exception as exc:  # noqa: BLE001 — любую ошибку старта трактуем как «нет Docker»
        _pg_unavailable(f"Docker не запущен или контейнер не стартовал ({exc})")
        return

    try:
        yield libpq_dsn(container)
    finally:
        container.stop()
