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
from app.forge_machines import FORGE_MACHINES  # noqa: E402
from app.main import app  # noqa: E402
from app.trajectory import measure, noise_source  # noqa: E402

AUTH = (settings.BASIC_AUTH_USER, settings.BASIC_AUTH_PASSWORD)
BAD_AUTH = ("wrong-user", "wrong-password")

_KNOWN_MACHINE = FORGE_MACHINES[0]


def _ingest_payload() -> dict:
    """Валидное измерение для известного агрегата из module траектории —
    в том же JSON-формате, что отправляет emulator/forge_stream.py."""
    m = measure(_KNOWN_MACHINE, 1.0, datetime.now(timezone.utc),
                noise_source(_KNOWN_MACHINE))
    payload = m.model_dump()
    payload["timestamp"] = m.timestamp.isoformat()
    return payload


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


# --------------------------------------------------------------- #
# Публичный демо-стенд (SPA_PUBLIC_DASHBOARD=true): чтение открыто,
# запись по-прежнему только с учётными данными.
# --------------------------------------------------------------- #
@pytest.fixture
def public_dashboard():
    original = settings.PUBLIC_DASHBOARD
    settings.PUBLIC_DASHBOARD = True
    try:
        yield
    finally:
        settings.PUBLIC_DASHBOARD = original


def test_public_dashboard_off_by_default():
    assert settings.PUBLIC_DASHBOARD is False


@pytest.mark.parametrize("path", ["/", "/equipment", "/predictions/overview"])
def test_public_dashboard_opens_reads(client, public_dashboard, path):
    r = client.get(path)
    assert r.status_code == 200


def test_public_dashboard_keeps_writes_closed(client, public_dashboard):
    r = client.post("/ingest", json=_ingest_payload())
    assert r.status_code == 401

    r = client.put("/settings/notifications", json={})
    assert r.status_code == 401

    r = client.post("/ingest", json=_ingest_payload(), auth=AUTH)
    assert r.status_code in (200, 202), r.text
