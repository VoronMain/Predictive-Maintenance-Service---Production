# -*- coding: utf-8 -*-
"""
status_test.py — статус агрегата согласован во всех ответах (issue #21).

Единственный seam — HTTP-ответы приложения: строка обзора предсказаний,
метаданные агрегата и счётчики дашборда обязаны давать один и тот же
статус. Состояние готовится записью в хранилище через его interface
(справочник агрегатов, предсказания). Модуль статуса напрямую не
тестируется. БД SQLite — во временном каталоге (SPA_DB_PATH до импорта).
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

_TMP = Path(tempfile.mkdtemp(prefix="spa_status_test_"))
os.environ.setdefault("SPA_DB_PATH", str(_TMP / "spa.db"))
os.environ.setdefault("SPA_DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.schema import EquipmentRecord, PredictionRecord  # noqa: E402
from tests.http_test import AUTH, _ingest_payload  # noqa: E402

THRESHOLD = 0.4


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fixed_threshold(monkeypatch):
    monkeypatch.setattr(settings, "FAILURE_THRESHOLD", THRESHOLD)


def _register(machine_id: str, hours: float, probability: float | None) -> None:
    db = app.state.db
    db.upsert_equipment(
        EquipmentRecord(machine_id=machine_id, machine_type="CNC", operational_hours=hours)
    )
    if probability is not None:
        db.insert_prediction(
            PredictionRecord(
                machine_id=machine_id,
                timestamp=datetime.now(timezone.utc),
                failure_probability=probability,
                # Метка отказа для статуса не используется: намеренно
                # противоречит вероятности.
                failure_label=0,
                remaining_useful_life_days=30.0,
                threshold=0.99,
            )
        )


def _row(client, machine_id: str) -> dict:
    rows = client.get("/predictions/overview", auth=AUTH).json()
    return next(r for r in rows if r["machine_id"] == machine_id)


def _card(client, machine_id: str) -> dict:
    r = client.get(f"/equipment/{machine_id}", auth=AUTH)
    assert r.status_code == 200
    return r.json()


def _stats(client) -> dict:
    return client.get("/dashboard/stats", auth=AUTH).json()


@pytest.mark.parametrize(
    "machine_id, hours, probability, expected",
    [
        ("ST-NEW-NOPRED", 5000.0, None, "new"),
        ("ST-NEW-YOUNG", 500.0, 0.9, "new"),
        ("ST-NEW-999", 999.9, 0.9, "new"),
        ("ST-NORMAL-1000", 1000.0, 0.1, "normal"),
        ("ST-NORMAL-BELOW", 5000.0, THRESHOLD - 0.001, "normal"),
        ("ST-PRE-EQUAL", 5000.0, THRESHOLD, "pre_failure"),
        ("ST-PRE-1000", 1000.0, 0.95, "pre_failure"),
    ],
)
def test_row_and_card_agree_on_status(client, machine_id, hours, probability, expected):
    _register(machine_id, hours, probability)
    assert _row(client, machine_id)["status"] == expected
    assert _card(client, machine_id)["status"] == expected


def test_dashboard_counters_equal_overview_rows(client):
    _register("ST-MIX-NEW", 100.0, None)
    _register("ST-MIX-NORMAL", 3000.0, 0.05)
    _register("ST-MIX-PRE", 3000.0, 0.9)

    rows = client.get("/predictions/overview", auth=AUTH).json()
    stats = _stats(client)

    for status in ("new", "normal", "pre_failure"):
        assert stats["by_status"][status] == sum(r["status"] == status for r in rows)
        assert stats["by_status"][status] >= 1
    assert stats["total"] == len(rows)
    assert set(stats["by_status"]) == {"new", "normal", "pre_failure"}


def test_threshold_change_changes_status_without_data_change(client, monkeypatch):
    _register("ST-THR", 5000.0, 0.5)
    assert _row(client, "ST-THR")["status"] == "pre_failure"
    before = _stats(client)["by_status"]

    monkeypatch.setattr(settings, "FAILURE_THRESHOLD", 0.6)

    assert _row(client, "ST-THR")["status"] == "normal"
    assert _card(client, "ST-THR")["status"] == "normal"
    after = _stats(client)["by_status"]
    assert after["pre_failure"] < before["pre_failure"]
    rows = client.get("/predictions/overview", auth=AUTH).json()
    for status in ("new", "normal", "pre_failure"):
        assert after[status] == sum(r["status"] == status for r in rows)


def test_status_uses_latest_prediction(client):
    _register("ST-LATEST", 5000.0, 0.9)
    assert _card(client, "ST-LATEST")["status"] == "pre_failure"
    _register("ST-LATEST", 5000.0, 0.1)
    assert _row(client, "ST-LATEST")["status"] == "normal"
    assert _card(client, "ST-LATEST")["status"] == "normal"


def test_unknown_machine_card_is_new_with_200(client):
    r = client.get("/equipment/ST-DOES-NOT-EXIST", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "new"


def test_existing_fields_kept(client):
    _register("ST-FIELDS", 5000.0, 0.2)
    row = _row(client, "ST-FIELDS")
    for key in ("failure_probability", "remaining_useful_life_days", "operational_hours"):
        assert key in row
    card = _card(client, "ST-FIELDS")
    for key in ("display_name", "machine_type", "category", "operational_hours"):
        assert key in card


def test_ingest_does_not_reset_operational_hours(client):
    payload = _ingest_payload()
    payload["machine_id"] = "ST-INGEST"
    payload["operational_hours"] = 4321.0

    r = client.post("/ingest", json=payload, auth=AUTH)
    assert r.status_code in (200, 202), r.text

    assert _card(client, "ST-INGEST")["operational_hours"] == 4321.0
    assert _row(client, "ST-INGEST")["operational_hours"] == 4321.0
