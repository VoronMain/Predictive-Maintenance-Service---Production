# -*- coding: utf-8 -*-
"""
notifications_test.py — тест политики оповещений (issue #29).

Единственный seam — interface модуля оповещений
(app.notifications.NotificationService): тест подаёт исходы детектора
инцидентов (IncidentResult) и управляет временем через «часы», а
проверяет записи журнала оповещений (чтение хранилища) и сообщения,
которые получили записывающие транспорты. Группировщик, кэш и вёрстку
писем напрямую не проверяет.

Окружение: настоящий SQLite во временном каталоге, записывающие
транспорты почты / SMS / push, управляемые часы. Моделей, конвейера и
HTTP не нужно, `sleep` не используется.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.database import SQLiteDatabase  # noqa: E402
from app.incidents import IncidentEvent, IncidentResult  # noqa: E402
from app.notifications import NotificationService  # noqa: E402
from app.schema import EquipmentRecord, PredictionRecord  # noqa: E402
from app.severity import Severity  # noqa: E402

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_INSPECTOR = "inspector@example.local"
_PHONE = "+70000000000"

# Вероятность по критичности при пороге t* = 0.33 и RUL = 30 дн.
_PROB = {Severity.MEDIUM: 0.40, Severity.HIGH: 0.55, Severity.CRITICAL: 0.80}


class FakeClock:
    """Управляемые часы: время стоит, пока тест не сдвинет его."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class RecordingMail:
    """Записывающий почтовый транспорт."""

    def __init__(self) -> None:
        self.sent: list = []
        self.fail_with: Optional[str] = None

    def send(self, msg) -> str:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.sent.append(msg)
        return "recorded"


class RecordingChannel:
    """Записывающий демонстрационный канал (SMS / push)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with: Optional[str] = None

    def send(self, recipient: str, subject: str, body: str) -> str:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.sent.append((recipient, subject))
        return "recorded"


class Stand:
    def __init__(self, tmp: Path) -> None:
        self.db = SQLiteDatabase(tmp / "spa.db")
        self.clock = FakeClock(_T0)
        self.mail = RecordingMail()
        self.sms = RecordingChannel()
        self.push = RecordingChannel()
        self.save_settings()
        self.service = self.new_service()
        self._types: dict[str, str] = {}

    def new_service(self) -> NotificationService:
        """Новый экземпляр на той же базе — имитация рестарта стенда."""
        return NotificationService(
            db=self.db, transport=self.mail, sender="spa@example.local",
            recipient="fallback@example.local", mode="file",
            clock=self.clock, sms_transport=self.sms, push_transport=self.push,
        )

    def save_settings(self, *, email=True, sms=False, push=False,
                      threshold: float = 0.33) -> None:
        self.db.save_notification_settings(
            email_enabled=email, sms_enabled=sms, push_enabled=push,
            email=_INSPECTOR, phone=_PHONE, failure_threshold=threshold,
        )

    def machine(self, machine_id: str, machine_type: str = "Press") -> str:
        self.db.upsert_equipment(EquipmentRecord(
            machine_id=machine_id, machine_type=machine_type,
            operational_hours=1000.0,
        ))
        self._types[machine_id] = machine_type
        return machine_id

    def incident(self, machine_id: str, severity: Severity) -> int:
        return self.db.open_incident(
            machine_id=machine_id, opened_at=self.clock(),
            probability=_PROB[severity], rul_days=30.0, threshold=0.33,
            severity=severity.value,
        )

    def event(self, machine_id: str, event: IncidentEvent,
              severity: Severity, *, at: Optional[datetime] = None,
              incident_id: Optional[int] = None,
              probability: Optional[float] = None) -> Optional[int]:
        """Подаёт модулю исход детектора для одного предсказания."""
        if incident_id is None and event in (IncidentEvent.OPENED,
                                             IncidentEvent.UPDATED):
            incident_id = self.incident(machine_id, severity)
        prediction = PredictionRecord(
            machine_id=machine_id, timestamp=at or self.clock(),
            failure_probability=(probability if probability is not None
                                 else _PROB.get(severity, 0.1)),
            failure_label=int(event in (IncidentEvent.OPENED,
                                        IncidentEvent.UPDATED)),
            remaining_useful_life_days=30.0, threshold=0.33,
        )
        return self.service.handle(
            prediction, self._types[machine_id],
            IncidentResult(event, incident_id, severity),
        )

    def alerts(self) -> list[dict]:
        return self.db.list_alerts(limit=1000)


@pytest.fixture()
def stand(tmp_path) -> Stand:
    return Stand(tmp_path)


OPENED, UPDATED = IncidentEvent.OPENED, IncidentEvent.UPDATED
MEDIUM, HIGH, CRITICAL = Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL


# ----- Событие инцидента ------------------------------------------------

def test_opened_incident_alerts_ignoring_suppression_window(stand):
    m = stand.machine("M1")
    stand.event(m, OPENED, CRITICAL)
    assert len(stand.mail.sent) == 1

    stand.clock.advance(seconds=30)
    stand.event(m, OPENED, CRITICAL, at=stand.clock())  # новый инцидент
    assert len(stand.mail.sent) == 2


def test_updated_incident_respects_window_of_its_severity(stand):
    m = stand.machine("M1")
    inc = stand.incident(m, CRITICAL)
    stand.event(m, OPENED, CRITICAL, incident_id=inc)
    assert len(stand.mail.sent) == 1

    stand.clock.advance(minutes=4)
    stand.event(m, UPDATED, CRITICAL, incident_id=inc)
    assert len(stand.mail.sent) == 1  # окно critical — 5 мин

    stand.clock.advance(minutes=2)
    stand.event(m, UPDATED, CRITICAL, incident_id=inc)
    assert len(stand.mail.sent) == 2


def test_updated_window_follows_new_severity(stand):
    m = stand.machine("M1")
    inc = stand.incident(m, HIGH)
    stand.event(m, OPENED, HIGH, incident_id=inc)
    stand.clock.advance(seconds=61)
    stand.service.flush_due()
    assert len(stand.mail.sent) == 1

    stand.clock.advance(minutes=6)  # окно high (15) не прошло, critical (5) — да
    stand.event(m, UPDATED, HIGH, incident_id=inc)
    assert len(stand.mail.sent) == 1
    stand.event(m, UPDATED, CRITICAL, incident_id=inc)
    assert len(stand.mail.sent) == 2


def test_closed_and_none_events_do_not_alert(stand):
    m = stand.machine("M1")
    stand.event(m, IncidentEvent.CLOSED, Severity.NONE, incident_id=None)
    stand.event(m, IncidentEvent.NONE, Severity.NONE)
    stand.service.flush_all()
    assert stand.mail.sent == []
    assert stand.alerts() == []


def test_normal_severity_does_not_alert(stand):
    m = stand.machine("M1")
    stand.event(m, OPENED, Severity.NONE, incident_id=stand.incident(m, MEDIUM))
    stand.service.flush_all()
    assert stand.mail.sent == []


# ----- Порог и каналы ---------------------------------------------------

def test_inspector_threshold_cuts_weaker_states(stand):
    stand.save_settings(threshold=0.7)
    m = stand.machine("M1")
    stand.event(m, OPENED, HIGH)  # p = 0.55 < 0.7
    stand.service.flush_all()
    assert stand.mail.sent == [] and stand.alerts() == []

    stand.event(m, OPENED, CRITICAL)  # p = 0.80 >= 0.7
    assert len(stand.mail.sent) == 1


def test_all_channels_off_forms_no_alerts_and_no_log(stand):
    stand.save_settings(email=False)
    m = stand.machine("M1")
    stand.event(m, OPENED, CRITICAL)
    stand.service.flush_all()
    assert stand.mail.sent == [] and stand.sms.sent == [] and stand.push.sent == []
    assert stand.alerts() == []


def test_only_sms_enabled_sends_no_mail_but_logs_sms(stand):
    stand.save_settings(email=False, sms=True)
    m = stand.machine("M1")
    stand.event(m, OPENED, CRITICAL)
    assert stand.mail.sent == []
    assert [r[0] for r in stand.sms.sent] == [_PHONE]
    rows = stand.alerts()
    assert [(r["channel"], r["status"]) for r in rows] == [("sms", "sent")]


def test_mail_goes_to_inspector_address_with_subject(stand):
    m = stand.machine("M1")
    stand.event(m, OPENED, CRITICAL)
    msg = stand.mail.sent[0]
    assert msg["To"] == _INSPECTOR
    assert "M1" in msg["Subject"] and "critical" in msg["Subject"]


# ----- Ошибки доставки --------------------------------------------------

def test_failed_delivery_is_logged_and_does_not_start_window(stand):
    m = stand.machine("M1")
    inc = stand.incident(m, CRITICAL)
    stand.mail.fail_with = "SMTP down"
    stand.event(m, OPENED, CRITICAL, incident_id=inc)
    rows = stand.alerts()
    assert [(r["status"], r["error"]) for r in rows] == [("failed", "SMTP down")]

    stand.mail.fail_with = None
    stand.clock.advance(seconds=30)  # внутри окна critical, но отправки не было
    stand.event(m, UPDATED, CRITICAL, incident_id=inc)
    assert len(stand.mail.sent) == 1
    assert sorted(r["status"] for r in stand.alerts()) == ["failed", "sent"]


# ----- Группировка ------------------------------------------------------

def test_critical_is_delivered_immediately(stand):
    stand.event(stand.machine("M1"), OPENED, CRITICAL)
    assert len(stand.mail.sent) == 1


def test_same_type_and_severity_are_grouped_into_one_mail(stand):
    stand.event(stand.machine("M1"), OPENED, HIGH)
    stand.event(stand.machine("M2"), OPENED, HIGH)
    assert stand.mail.sent == []

    stand.clock.advance(seconds=61)
    stand.service.flush_due()  # новых измерений нет — группа уходит по часам
    assert len(stand.mail.sent) == 1
    assert "Сводное" in stand.mail.sent[0]["Subject"]


def test_different_types_or_severities_make_separate_mails(stand):
    stand.event(stand.machine("M1", "Press"), OPENED, HIGH)
    stand.event(stand.machine("M2", "Lathe"), OPENED, HIGH)
    stand.event(stand.machine("M3", "Press"), OPENED, MEDIUM)
    stand.clock.advance(seconds=61)
    stand.service.flush_due()
    assert len(stand.mail.sent) == 3
    assert not any("Сводное" in m["Subject"] for m in stand.mail.sent)


def test_flush_all_on_shutdown_delivers_pending_groups(stand):
    stand.event(stand.machine("M1"), OPENED, HIGH)
    stand.event(stand.machine("M2"), OPENED, HIGH)
    assert stand.mail.sent == []
    stand.service.flush_all()
    assert len(stand.mail.sent) == 1


# ----- Рестарт и журнал сводных писем ----------------------------------

def _grouped_stand(stand: Stand) -> dict[str, int]:
    incidents = {}
    for name in ("M1", "M2", "M3"):
        m = stand.machine(name)
        incidents[m] = stand.incident(m, HIGH)
        stand.event(m, OPENED, HIGH, incident_id=incidents[m])
    stand.clock.advance(seconds=61)
    stand.service.flush_due()
    assert len(stand.mail.sent) == 1
    return incidents


def test_suppression_window_survives_restart_for_every_group_member(stand):
    incidents = _grouped_stand(stand)
    restarted = stand.new_service()
    stand.service = restarted

    stand.clock.advance(minutes=5)
    for m, inc in incidents.items():
        stand.event(m, UPDATED, HIGH, incident_id=inc)
    restarted.flush_all()
    assert len(stand.mail.sent) == 1  # окно high (15 мин) соблюдено для всех

    stand.clock.advance(minutes=11)
    for m, inc in incidents.items():
        stand.event(m, UPDATED, HIGH, incident_id=inc)
    restarted.flush_all()
    assert len(stand.mail.sent) == 2


def test_group_alert_shows_every_member_with_its_incident(stand):
    incidents = _grouped_stand(stand)
    rows = stand.alerts()
    assert len(rows) == 1  # одна попытка доставки — одна запись
    members = {m["machine_id"]: m["incident_id"] for m in rows[0]["members"]}
    assert members == incidents
    assert stand.db.get_alert(rows[0]["id"])["members"] == rows[0]["members"]


def test_alerts_counter_counts_deliveries_not_aggregates(stand):
    _grouped_stand(stand)
    assert stand.db.get_alerts_count() == 1


def test_failed_sms_is_logged_with_error_and_does_not_start_window(stand):
    stand.save_settings(email=False, sms=True)
    m = stand.machine("M1")
    inc = stand.incident(m, CRITICAL)
    stand.sms.fail_with = "gateway down"
    stand.event(m, OPENED, CRITICAL, incident_id=inc)
    assert [(r["channel"], r["status"], r["error"]) for r in stand.alerts()] == [
        ("sms", "failed", "gateway down")]

    stand.sms.fail_with = None
    stand.clock.advance(seconds=30)
    stand.event(m, UPDATED, CRITICAL, incident_id=inc)
    assert len(stand.sms.sent) == 1


def test_settings_change_between_queueing_and_flush_applies_to_delivery(stand):
    stand.event(stand.machine("M1"), OPENED, HIGH)
    stand.event(stand.machine("M2"), OPENED, HIGH)
    stand.save_settings(email=False, sms=True)
    stand.clock.advance(seconds=61)
    stand.service.flush_due()
    assert stand.mail.sent == []
    assert len(stand.sms.sent) == 1
