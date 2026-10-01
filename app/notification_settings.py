# -*- coding: utf-8 -*-
"""notification_settings.py — значения настроек оповещений по умолчанию.

Единственное место, где записано правило «если настроек оповещений нет —
включена только почта на адрес из конфигурации, порог оповещений = t*».
Adapters хранилища этого правила не знают: при отсутствии записи они
возвращают None, а значения подставляет load_notification_settings().
Запасная конфигурация сервиса оповещений (при ошибке чтения настроек)
берёт те же значения через default_notification_settings().
"""
from __future__ import annotations

from typing import Optional

from .config import settings
from .db_base import DatabaseProtocol


def default_notification_settings(email: Optional[str] = None) -> dict:
    """Настройки по умолчанию: только email, порог = t* из конфигурации.

    email — адрес получателя; по умолчанию SPA_SMTP_TO из конфигурации.
    """
    return {
        "email_enabled": True,
        "sms_enabled": False,
        "push_enabled": False,
        "email": settings.SMTP_TO if email is None else email,
        "phone": "",
        "failure_threshold": settings.FAILURE_THRESHOLD,
        "updated_at": None,
    }


def load_notification_settings(db: DatabaseProtocol) -> dict:
    """Текущие настройки оповещений; при отсутствии записи — значения по умолчанию."""
    stored = db.get_notification_settings()
    if stored is None:
        return default_notification_settings()
    if stored.get("failure_threshold") is None:
        stored["failure_threshold"] = settings.FAILURE_THRESHOLD
    return stored
