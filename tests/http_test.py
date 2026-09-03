# -*- coding: utf-8 -*-
"""
http_test.py — тест HTTP-интерфейса СПА: авторизация доступа к стенду.

Проверяет закрытие публичного доступа паролем (issue #2): любой маршрут,
кроме /health и /static/*, требует HTTP Basic авторизацию; JSON-эндпоинты
и /ingest по-прежнему работают с корректными учётными данными; защита от
учётных данных-заглушек (SPA_REQUIRE_STRONG_AUTH) отказывает в старте
приложения, когда режим включён и заданы дефолтные логин/пароль, и не
влияет на запуск, когда режим выключен (значение по умолчанию).

Тест проверяет только внешнее поведение (коды ответов), а не то, как
именно навешана зависимость авторизации. Использует TestClient(app) без
поднятия сетевого порта; БД SQLite перенаправлена во временный каталог
через SPA_DB_PATH до импорта приложения.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Перенаправляем БД во временный каталог, чтобы не задеть основной файл.
_TMP = Path(tempfile.mkdtemp(prefix="spa_http_test_"))
os.environ["SPA_DB_PATH"] = str(_TMP / "spa.db")
os.environ.setdefault("SPA_DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.forge_machines import FORGE_MACHINES, generate_sensor_values  # noqa: E402
from app.main import app  # noqa: E402

AUTH = (settings.BASIC_AUTH_USER, settings.BASIC_AUTH_PASSWORD)
BAD_AUTH = ("wrong-user", "wrong-password")

_KNOWN_MACHINE = FORGE_MACHINES[0]


def _ingest_payload() -> dict:
    """Валидное измерение для известного агрегата — тот же формат,
    что использует emulator/forge_stream.py."""
    sensors = generate_sensor_values(_KNOWN_MACHINE, t=1.0, rng=random.Random(42))
    return {
        "machine_id": _KNOWN_MACHINE.machine_id,
        "machine_type": _KNOWN_MACHINE.ml_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operational_hours": _KNOWN_MACHINE.operational_hours,
        "temperature_c": sensors["temperature_c"],
        "vibration_mms": sensors["vibration_mms"],
        "sound_db": sensors["sound_db"],
        "oil_level_pct": sensors["oil_level_pct"],
        "coolant_level_pct": sensors["coolant_level_pct"],
        "power_consumption_kw": sensors["power_consumption_kw"],
        "last_maintenance_days_ago": 10,
        "maintenance_history_count": _KNOWN_MACHINE.maintenance_history_count,
        "failure_history_count": _KNOWN_MACHINE.failure_history_count,
        "ai_supervision": True,
        "error_codes_last_30_days": sensors["error_codes_last_30_days"],
        "ai_override_events": sensors["ai_override_events"],
        "laser_intensity": None,
        "hydraulic_pressure_bar": None,
        "coolant_flow_l_min": None,
        "heat_index": None,
    }


# --------------------------------------------------------------- #
# Защита от учётных данных-заглушек (SPA_REQUIRE_STRONG_AUTH).
#
# Эти тесты сами открывают и закрывают TestClient (полный жизненный
# цикл lifespan за пределы функции не выходит), поэтому расположены
# раньше фикстуры `client` ниже: на момент их выполнения общий
# module-scoped TestClient ещё не создан, и приложение не запускается
# параллельно само на себя.
# --------------------------------------------------------------- #
def test_strong_auth_off_by_default_starts_with_stub_credentials():
    """По умолчанию SPA_REQUIRE_STRONG_AUTH выключен — приложение
    стартует как раньше даже с дефолтными admin/admin."""
    assert settings.REQUIRE_STRONG_AUTH is False
    assert settings.BASIC_AUTH_USER == "admin"
    assert settings.BASIC_AUTH_PASSWORD == "admin"

    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200


def test_strong_auth_blocks_stub_credentials_on_startup():
    """При SPA_REQUIRE_STRONG_AUTH=true и учётных данных из стоп-списка
    (admin/admin) приложение отказывается стартовать."""
    original = (settings.REQUIRE_STRONG_AUTH,
                settings.BASIC_AUTH_USER, settings.BASIC_AUTH_PASSWORD)
    settings.REQUIRE_STRONG_AUTH = True
    settings.BASIC_AUTH_USER = "admin"
    settings.BASIC_AUTH_PASSWORD = "admin"
    try:
        with pytest.raises(RuntimeError, match="SPA_REQUIRE_STRONG_AUTH"):
            with TestClient(app):
                pass
    finally:
        (settings.REQUIRE_STRONG_AUTH,
         settings.BASIC_AUTH_USER, settings.BASIC_AUTH_PASSWORD) = original


def test_strong_auth_allows_non_stub_credentials_on_startup():
    """При SPA_REQUIRE_STRONG_AUTH=true, но нетривиальных логине и
    пароле, приложение стартует нормально."""
    original = (settings.REQUIRE_STRONG_AUTH,
                settings.BASIC_AUTH_USER, settings.BASIC_AUTH_PASSWORD)
    settings.REQUIRE_STRONG_AUTH = True
    settings.BASIC_AUTH_USER = "forge-inspector"
    settings.BASIC_AUTH_PASSWORD = "Kj9#mZ2pQxL7"
    try:
        with TestClient(app) as c:
            r = c.get("/health")
            assert r.status_code == 200
    finally:
        (settings.REQUIRE_STRONG_AUTH,
         settings.BASIC_AUTH_USER, settings.BASIC_AUTH_PASSWORD) = original


# --------------------------------------------------------------- #
# Общий клиент для проверок доступа. Создаётся один раз на модуль —
# приложение уже засеяно к этому моменту тестами выше, повторный старт
# быстрый (засев истории пропускается).
# --------------------------------------------------------------- #
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------- #
# /health — единственный маршрут, открытый без авторизации
# --------------------------------------------------------------- #
def test_health_open_without_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# --------------------------------------------------------------- #
# HTML-страницы веб-интерфейса — закрыты
# --------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/", "/incidents-ui", "/settings"])
def test_html_pages_require_auth(client, path):
    r = client.get(path)
    assert r.status_code == 401

    r = client.get(path, auth=AUTH)
    assert r.status_code == 200


def test_machine_page_requires_auth(client):
    path = f"/machine/{_KNOWN_MACHINE.machine_id}"

    r = client.get(path)
    assert r.status_code == 401

    r = client.get(path, auth=AUTH)
    assert r.status_code == 200


def test_wrong_credentials_rejected(client):
    r = client.get("/", auth=BAD_AUTH)
    assert r.status_code == 401


# --------------------------------------------------------------- #
# Документация API — закрыта авторизацией (не выключена)
# --------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_require_auth(client, path):
    r = client.get(path)
    assert r.status_code == 401

    r = client.get(path, auth=AUTH)
    assert r.status_code == 200


# --------------------------------------------------------------- #
# JSON-эндпоинты — по-прежнему защищены
# --------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/equipment", "/predictions/overview", "/info"])
def test_json_endpoints_require_auth(client, path):
    r = client.get(path)
    assert r.status_code == 401

    r = client.get(path, auth=AUTH)
    assert r.status_code == 200


def test_ingest_requires_auth_and_works_with_credentials(client):
    r = client.post("/ingest", json=_ingest_payload())
    assert r.status_code == 401

    r = client.post("/ingest", json=_ingest_payload(), auth=AUTH)
    assert r.status_code in (200, 202), r.text


# --------------------------------------------------------------- #
# Статика — открыта; нужна, чтобы закрытые страницы отрисовывались
# после ввода пароля.
# --------------------------------------------------------------- #
def test_static_asset_open_without_auth(client):
    r = client.get("/static/spa.css")
    assert r.status_code == 200
