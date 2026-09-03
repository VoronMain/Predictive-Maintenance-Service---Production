# -*- coding: utf-8 -*-
"""config.py - модуль конфигурации СПА.

Все чувствительные параметры и пути инжектируются через переменные
окружения файла .env. При отсутствии переменных применяются значения
по умолчанию, рассчитанные на локальный запуск демонстрационного стенда.

Подсистема хранения данных поддерживает два взаимозаменяемых бэкенда,
выбираемых переменной SPA_DB_BACKEND:

  * postgres — производственный бэкенд на базе PostgreSQL с расширением
    TimescaleDB (гипертаблицы, политики ретенции и сжатия);
  * sqlite   — резервный бэкенд на встроенной СУБД SQLite, применяемый
    при автономном запуске демонстрационного стенда и при выполнении
    автоматизированных тестов без поднятия серверной СУБД.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Параметры запуска приложения СПА."""

    # ------------------------------------------------------------ #
    # Подсистема хранения данных
    # ------------------------------------------------------------ #
    DB_BACKEND: str = os.getenv("SPA_DB_BACKEND", "sqlite").lower()
    DB_PATH: Path = Path(os.getenv("SPA_DB_PATH", ROOT_DIR / "data" / "spa.db"))

    PG_HOST: str = os.getenv("SPA_PG_HOST", "127.0.0.1")
    PG_PORT: int = int(os.getenv("SPA_PG_PORT", "5432"))
    PG_DB: str = os.getenv("SPA_PG_DB", "spa")
    PG_USER: str = os.getenv("SPA_PG_USER", "spa")
    PG_PASSWORD: str = os.getenv("SPA_PG_PASSWORD", "spa")
    PG_RAW_RETENTION_DAYS: int = int(os.getenv("SPA_PG_RAW_RETENTION_DAYS", "30"))
    PG_AGG_RETENTION_DAYS: int = int(
        os.getenv("SPA_PG_AGG_RETENTION_DAYS", str(365 * 5))
    )
    PG_RAW_COMPRESS_AFTER_DAYS: int = int(
        os.getenv("SPA_PG_RAW_COMPRESS_AFTER_DAYS", "7")
    )
    PG_AGG_COMPRESS_AFTER_DAYS: int = int(
        os.getenv("SPA_PG_AGG_COMPRESS_AFTER_DAYS", "30")
    )

    @property
    def pg_dsn(self) -> str:
        """Строка подключения к PostgreSQL в формате libpq."""
        return (
            f"host={self.PG_HOST} port={self.PG_PORT} dbname={self.PG_DB} "
            f"user={self.PG_USER} password={self.PG_PASSWORD}"
        )

    # ------------------------------------------------------------ #
    # Артефакты моделей и датасет
    # ------------------------------------------------------------ #
    # Пакет predictive_maintenance вместе с артефактами models/ вкомпилирован
    # в дерево репозитория, поэтому дефолт по умолчанию указывает именно
    # туда — это то, что реально попадает в Docker-образ. Внешний каталог
    # Модели (соседний с репозиторием на машине автора) остаётся резервным
    # вариантом ради обратной совместимости локального запуска: если он
    # существует, а SPA_MODELS_DIR не задана явно, используется он — как и
    # раньше. Явно заданная SPA_MODELS_DIR всегда главнее обоих дефолтов.
    _BUILTIN_MODELS_DIR: Path = ROOT_DIR / "predictive_maintenance" / "models"
    _EXTERNAL_MODELS_DIR: Path = (
        ROOT_DIR.parent / "Модели" / "predictive_maintenance" / "models"
    )
    MODELS_DIR: Path = Path(
        os.getenv(
            "SPA_MODELS_DIR",
            _EXTERNAL_MODELS_DIR
            if _EXTERNAL_MODELS_DIR.is_dir()
            else _BUILTIN_MODELS_DIR,
        )
    )
    DATASET_PATH: Path = Path(
        os.getenv(
            "SPA_DATASET_PATH",
            ROOT_DIR.parent / "Датасет" / "factory_sensor_simulator_2040.csv",
        )
    )
    AGGREGATION_WINDOW_SECONDS: int = int(
        os.getenv("SPA_AGGREGATION_WINDOW_SECONDS", "3600")
    )
    FAILURE_THRESHOLD: float = float(os.getenv("SPA_FAILURE_THRESHOLD", "0.33"))

    # ------------------------------------------------------------ #
    # HTTP-сервис и аутентификация
    # ------------------------------------------------------------ #
    API_HOST: str = os.getenv("SPA_API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("SPA_API_PORT", "8000"))
    BASIC_AUTH_USER: str = os.getenv("SPA_BASIC_AUTH_USER", "admin")
    BASIC_AUTH_PASSWORD: str = os.getenv("SPA_BASIC_AUTH_PASSWORD", "admin")
    # Строгий режим: отказ от старта с учётными данными-заглушками
    # (admin/admin, spa, пустое значение и т. п.). По умолчанию выключен,
    # иначе локальный запуск и автотесты с дефолтными admin/admin
    # перестанут работать. На публичном стенде (Railway) включается явно.
    REQUIRE_STRONG_AUTH: bool = _as_bool(os.getenv("SPA_REQUIRE_STRONG_AUTH", "false"))

    # ------------------------------------------------------------ #
    # Подсистема оповещений (email)
    # ------------------------------------------------------------ #
    SMTP_MODE: str = os.getenv("SPA_SMTP_MODE", "file").lower()
    SMTP_HOST: str = os.getenv("SPA_SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SPA_SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SPA_SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SPA_SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _as_bool(os.getenv("SPA_SMTP_USE_TLS", "true"))
    SMTP_FROM: str = os.getenv("SPA_SMTP_FROM", "spa-alerts@example.local")
    SMTP_TO: str = os.getenv("SPA_SMTP_TO", "maintenance@example.local")
    ALERTS_DIR: Path = Path(
        os.getenv("SPA_ALERTS_DIR", ROOT_DIR / "data" / "alerts")
    )

    # ------------------------------------------------------------ #
    # Динамическое подавление повторов и группировка оповещений
    # ------------------------------------------------------------ #
    ALERT_COOLDOWN_MINUTES: int = int(
        os.getenv("SPA_ALERT_COOLDOWN_MINUTES", "30")
    )
    COOLDOWN_CRITICAL_MINUTES: int = int(
        os.getenv("SPA_COOLDOWN_CRITICAL_MINUTES", "5")
    )
    COOLDOWN_HIGH_MINUTES: int = int(
        os.getenv("SPA_COOLDOWN_HIGH_MINUTES", "15")
    )
    COOLDOWN_MEDIUM_MINUTES: int = int(
        os.getenv("SPA_COOLDOWN_MEDIUM_MINUTES", "30")
    )
    SEVERITY_CRITICAL_PROB: float = float(
        os.getenv("SPA_SEVERITY_CRITICAL_PROB", "0.66")
    )
    SEVERITY_HIGH_PROB: float = float(
        os.getenv("SPA_SEVERITY_HIGH_PROB", "0.50")
    )
    SEVERITY_CRITICAL_RUL_DAYS: float = float(
        os.getenv("SPA_SEVERITY_CRITICAL_RUL_DAYS", "7")
    )
    SEVERITY_HIGH_RUL_DAYS: float = float(
        os.getenv("SPA_SEVERITY_HIGH_RUL_DAYS", "14")
    )
    ALERT_GROUP_WINDOW_SECONDS: int = int(
        os.getenv("SPA_ALERT_GROUP_WINDOW_SECONDS", "60")
    )


settings = Settings()
