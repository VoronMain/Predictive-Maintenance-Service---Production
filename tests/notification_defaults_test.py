# -*- coding: utf-8 -*-
"""
notification_defaults_test.py — значения настроек оповещений по умолчанию
задаются в одном месте (app/notification_settings.py), а не в adapters и
не копией внутри сервиса оповещений (issue #27).

Проверяется запасная конфигурация NotificationService: если настройки
прочитать не удалось, берутся те же значения по умолчанию, что отдаёт
эндпоинт настроек на пустой базе.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.notification_settings import (  # noqa: E402
    default_notification_settings,
    load_notification_settings,
)
from app.notifications import NotificationService  # noqa: E402


class _BrokenDb:
    def get_notification_settings(self):
        raise RuntimeError("база недоступна")


class _EmptyDb:
    def get_notification_settings(self):
        return None


def _service(db, recipient: str) -> NotificationService:
    return NotificationService(db=db, transport=None, sender="spa@example.local",
                               recipient=recipient, mode="file")


def test_fallback_on_read_error_uses_shared_defaults():
    service = _service(_BrokenDb(), recipient="ops@example.local")

    assert service._channel_config() == default_notification_settings(
        email="ops@example.local")


def test_fallback_keeps_only_email_enabled_with_threshold_from_config():
    config = _service(_BrokenDb(), recipient="ops@example.local")._channel_config()

    assert (config["email_enabled"], config["sms_enabled"], config["push_enabled"]) == (
        True, False, False)
    assert config["failure_threshold"] == default_notification_settings()["failure_threshold"]


def test_empty_storage_and_read_error_give_same_channels_and_threshold():
    on_empty = load_notification_settings(_EmptyDb())
    on_error = default_notification_settings(email=on_empty["email"])

    assert on_empty == on_error


def test_service_reads_defaults_from_empty_storage():
    config = _service(_EmptyDb(), recipient="ignored@example.local")._channel_config()

    assert config == default_notification_settings()
