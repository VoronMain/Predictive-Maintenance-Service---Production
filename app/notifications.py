# -*- coding: utf-8 -*-
"""
notifications.py — подсистема оповещений с динамическим подавлением
повторов и групповой агрегацией однотипных событий.

Подсистема формирует и доставляет уведомления о предаварийных
состояниях оборудования сотрудникам службы технического обслуживания
и ремонта (ТОиР). Она — единственный владелец решения «оповещать ли,
когда и кого»: конвейер передаёт ей исход детектора инцидентов как
есть (NotificationService.handle), а всю политику она применяет сама,
по шагам:

  1. События инцидента «закрыт» и «нет инцидента», а также критичность
     «норма» оповещений не порождают. Критичность не пересчитывается —
     берётся та, что вернул детектор инцидентов (app/incidents.py).
  2. Порог оповещения из настроек инспектора: вероятность ниже порога
     (по умолчанию t*) оповещения не порождает.
  3. Если в настройках не включён ни один канал, оповещения не
     формируются и в журнал не попадают.
  4. Подавление повторов. Открытие инцидента окно подавления игнорирует;
     продолжение инцидента оповещает не чаще окна для своей критичности
     (critical — самое короткое). Окно считается ПО АГРЕГАТУ, а не по
     инциденту: от времени события (метки предсказания) последней
     успешной отправки по этому агрегату. Время последней отправки
     читается из журнала оповещений (в том числе для агрегатов в составе
     сводного письма), поэтому окно переживает рестарт. Неудачная
     доставка отправкой не считается и окно не включает.
  5. Групповая агрегация. Прошедшие оповещения копятся по ключу «тип
     оборудования + критичность» и по истечении окна агрегации
     объединяются в одно сводное письмо; critical доставляется
     немедленно. Окно агрегации считается по «часам» — зависимости,
     которую приложение передаёт настоящей (UTC), а тест — управляемой.

Письма формируются в формате multipart/alternative (текстовая и HTML-
версии, app/email_render.py); в HTML-версию встраивается inline-график
динамики (через Content-ID), а сам график прикладывается к письму
отдельным файлом. Доставка идёт по каналам, включённым в настройках
(почта, SMS, push); настройки читаются при каждом решении и при
доставке. Транспорты (почта, SMS, push) передаются сервису извне.
Почтовых транспорта два, выбор — переменной SPA_SMTP_MODE: smtp
(внешний SMTP-сервер с TLS) и file (сохранение писем в виде .eml-файлов
в каталоге data/alerts для отладки и демонстрации). SMS и push —
демонстрационные заглушки, фиксирующие доставку в журнале.

В журнале оповещений (alerts_log) одна запись соответствует одной
попытке доставки по каналу. Состав сводного оповещения хранится в
alert_members: по записи на агрегат группы с его инцидентом и временем
события.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Optional

from . import charts, email_render
from .config import settings
from .db_base import DatabaseProtocol
from .incidents import IncidentEvent, IncidentResult
from .notification_settings import default_notification_settings, load_notification_settings
from .schema import PredictionRecord
from .severity import (
    Severity,
    cooldown_minutes,
    group_window_seconds,
)

log = logging.getLogger(__name__)

# «Часы» — источник текущего времени (UTC, с часовым поясом).
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Часы приложения: системное время UTC."""
    return datetime.now(timezone.utc)

_ACTIONS_BASE = [
    "Произвести визуальный осмотр единицы оборудования.",
    "Уточнить плановые сроки технического обслуживания.",
]
_ACTIONS_BY_SEVERITY = {
    Severity.MEDIUM: _ACTIONS_BASE + [
        "Поставить агрегат на усиленный мониторинг.",
    ],
    Severity.HIGH: _ACTIONS_BASE + [
        "Согласовать со службой ТОиР внеплановое обслуживание.",
        "Подготовить запасные части под прогнозируемый отказ.",
    ],
    Severity.CRITICAL: _ACTIONS_BASE + [
        "Незамедлительно оповестить сменного инженера ТОиР.",
        "Рассмотреть вывод агрегата из эксплуатации до устранения причины.",
        "Назначить внеплановый ремонт в приоритетном порядке.",
    ],
}
_ACTIONS_GROUP = [
    "Сформировать сводную ремонтную заявку по перечню оборудования.",
    "Распределить осмотр агрегатов между сменным персоналом ТОиР.",
    "Проконтролировать наличие запасных частей по затронутым типам узлов.",
]


@dataclass
class AlertItem:
    """Кандидат на оповещение, прошедший проверку подавления повторов."""

    prediction: PredictionRecord
    machine_type: str
    incident_id: Optional[int]
    severity: Severity
    event_time: datetime


@dataclass
class _Group:
    items: list = field(default_factory=list)
    deadline: Optional[datetime] = None


class AlertGrouper:
    """Группировщик однотипных оповещений (alert grouping).

    Накапливает оповещения, относящиеся к одному ключу группировки
    (тип оборудования + степень критичности), в пределах окна агрегации
    и передаёт их на доставку по истечении окна. Оповещения с нулевым
    окном (уровень critical) доставляются немедленно.
    """

    def __init__(self, deliver: Callable[[str, list], Optional[int]]) -> None:
        self._deliver = deliver
        self._groups: dict[str, _Group] = {}
        self._lock = threading.RLock()

    @staticmethod
    def group_key(machine_type: str, severity: Severity) -> str:
        return f"{machine_type}|{severity.value}"

    def submit(self, item: AlertItem, window_seconds: int,
               now: datetime) -> list[int]:
        key = self.group_key(item.machine_type, item.severity)
        with self._lock:
            grp = self._groups.get(key)
            if grp is None:
                grp = _Group(deadline=now + timedelta(seconds=window_seconds))
                self._groups[key] = grp
            grp.items.append(item)
            if window_seconds <= 0:
                return self._flush_key(key)
        return []

    def flush_due(self, now: datetime) -> list[int]:
        produced: list[int] = []
        with self._lock:
            due = [k for k, g in self._groups.items()
                   if g.deadline is not None and g.deadline <= now]
            for key in due:
                produced.extend(self._flush_key(key))
        return produced

    def flush_all(self) -> list[int]:
        produced: list[int] = []
        with self._lock:
            for key in list(self._groups.keys()):
                produced.extend(self._flush_key(key))
        return produced

    def pending(self) -> int:
        with self._lock:
            return sum(len(g.items) for g in self._groups.values())

    def _flush_key(self, key: str) -> list[int]:
        grp = self._groups.pop(key, None)
        if grp is None or not grp.items:
            return []
        alert_id = self._deliver(key, grp.items)
        return [alert_id] if alert_id is not None else []


class FileEmailTransport:
    """Транспорт сохранения писем в каталог (демонстрация, тестирование)."""

    def __init__(self, alerts_dir: Path) -> None:
        self.alerts_dir = Path(alerts_dir)
        self.alerts_dir.mkdir(parents=True, exist_ok=True)

    def send(self, msg: EmailMessage) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        safe = "".join(
            c if c.isalnum() or c in "._-" else "_"
            for c in str(msg["Subject"])[:60]
        )
        path = self.alerts_dir / f"{ts}_{safe}.eml"
        with open(path, "wb") as fh:
            fh.write(bytes(msg))
        log.info("Оповещение сохранено в файл: %s", path)
        return str(path)


class SMTPEmailTransport:
    """Транспорт отправки писем через внешний SMTP-сервер."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 use_tls: bool) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_tls = use_tls

    def send(self, msg: EmailMessage) -> str:
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            smtp.ehlo()
            if self.use_tls:
                ctx = ssl.create_default_context()
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
        log.info("Оповещение отправлено по SMTP на %s", msg["To"])
        return f"smtp://{self.host}:{self.port}"


class SmsStubTransport:
    """Демонстрационный транспорт SMS-канала.

    Реальная отправка SMS требует интеграции с внешним SMS-шлюзом и
    выходит за рамки настоящей работы. Транспорт регистрирует факт
    доставки в журнале оповещений (alerts_log, channel='sms'),
    подтверждая срабатывание выбранного канала в сквозном сценарии.
    """

    channel = "sms"

    def send(self, recipient: str, subject: str, body: str) -> str:
        log.info("Оповещение зафиксировано в канале SMS для %s: %s",
                 recipient or "—", subject)
        return f"sms://{recipient or 'unset'}"


class PushStubTransport:
    """Демонстрационный транспорт push-канала (см. SmsStubTransport)."""

    channel = "push"

    def send(self, recipient: str, subject: str, body: str) -> str:
        log.info("Оповещение зафиксировано в канале PUSH для %s: %s",
                 recipient or "—", subject)
        return f"push://{recipient or 'unset'}"


class NotificationService:
    """Сервис формирования и доставки оповещений.

    Объединяет проверку подавления повторов, группировку однотипных
    событий и формирование HTML-сообщений с вложениями.
    """

    def __init__(self, db: DatabaseProtocol, transport, sender: str, recipient: str,
                 mode: str, clock: Clock = utc_now, sms_transport=None,
                 push_transport=None) -> None:
        self.db = db
        self.transport = transport
        self.sender = sender
        self.recipient = recipient
        self.mode = mode
        self.clock = clock
        self.sms_transport = sms_transport or SmsStubTransport()
        self.push_transport = push_transport or PushStubTransport()
        self.grouper = AlertGrouper(deliver=self._deliver_group)

    # ----- Активные каналы и порог оповещения из настроек -----
    def _channel_config(self) -> dict:
        """Читает настройки оповещений (каналы, адреса, порог) из БД.

        При недоступности настроек применяется резервная конфигурация:
        включён только email на адрес из параметров приложения."""
        try:
            return load_notification_settings(self.db)
        except Exception:
            log.exception("Не удалось прочитать настройки оповещений")
            return default_notification_settings(email=self.recipient)

    @classmethod
    def from_settings(cls, db: DatabaseProtocol) -> "NotificationService":
        mode = settings.SMTP_MODE
        if mode == "smtp" and settings.SMTP_HOST:
            transport = SMTPEmailTransport(
                host=settings.SMTP_HOST, port=settings.SMTP_PORT,
                user=settings.SMTP_USER, password=settings.SMTP_PASSWORD,
                use_tls=settings.SMTP_USE_TLS,
            )
            effective_mode = "smtp"
        else:
            transport = FileEmailTransport(settings.ALERTS_DIR)
            effective_mode = "file"
            if mode == "smtp":
                log.warning("SPA_SMTP_MODE=smtp задан, но SMTP_HOST пуст. "
                            "Включён резервный file-режим (data/alerts/).")
        return cls(db=db, transport=transport, sender=settings.SMTP_FROM,
                   recipient=settings.SMTP_TO, mode=effective_mode)

    # ----- Подавление повторов -----
    def _last_sent_time(self, machine_id: str) -> Optional[datetime]:
        """Время события последней успешной отправки по агрегату — из
        журнала оповещений (единственный источник, переживает рестарт)."""
        row = self.db.last_alert_for_machine(machine_id)
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row["sent_at"])
        except (TypeError, ValueError):
            return None

    def _is_within_cooldown(self, machine_id: str, severity: Severity,
                            now: datetime) -> bool:
        last = self._last_sent_time(machine_id)
        if last is None:
            return False
        if last.tzinfo is None and now.tzinfo is not None:
            last = last.replace(tzinfo=now.tzinfo)
        elif last.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=last.tzinfo)
        window = timedelta(minutes=cooldown_minutes(severity))
        return (now - last) < window

    # ----- Приём оповещения и группировка -----
    def handle(self, prediction: PredictionRecord, machine_type: str,
               outcome: IncidentResult) -> Optional[int]:
        """Принимает исход детектора инцидентов для живого предсказания и
        применяет политику оповещений (см. docstring модуля). Возвращает
        идентификатор записи alerts_log при немедленной доставке либо
        None при отказе от оповещения, подавлении или помещении в группу
        до истечения окна агрегации."""
        if outcome.event not in (IncidentEvent.OPENED, IncidentEvent.UPDATED):
            return None
        severity = outcome.severity
        if not severity.is_alerting():
            return None
        incident_id = outcome.incident_id

        # Порог оповещения может быть повышен инспектором в настройках
        # профиля: при значении выше t* модели уведомления формируются
        # лишь для более выраженных предаварийных состояний.
        cfg = self._channel_config()
        alert_threshold = cfg.get("failure_threshold") or prediction.threshold
        if prediction.failure_probability < alert_threshold:
            return None
        if not (cfg.get("email_enabled") or cfg.get("sms_enabled")
                or cfg.get("push_enabled")):
            return None

        event_time = prediction.timestamp
        # Открытие инцидента окно подавления игнорирует, продолжение — нет.
        if outcome.event is IncidentEvent.UPDATED and self._is_within_cooldown(
                prediction.machine_id, severity, event_time):
            log.info("Оповещение по %s подавлено (severity=%s, окно %d мин)",
                     prediction.machine_id, severity.value,
                     cooldown_minutes(severity))
            return None

        item = AlertItem(
            prediction=prediction, machine_type=machine_type,
            incident_id=incident_id, severity=severity, event_time=event_time,
        )
        now = self.clock()
        window = group_window_seconds(severity)
        produced = self.grouper.submit(item, window, now)
        self.grouper.flush_due(now)
        return produced[0] if produced else None

    def flush_due(self) -> list[int]:
        """Доставляет группы, у которых истекло окно агрегации по часам."""
        return self.grouper.flush_due(self.clock())

    def flush_all(self) -> list[int]:
        return self.grouper.flush_all()

    # ----- Доставка (вызывается группировщиком) -----
    def _deliver_group(self, key: str, items: list) -> Optional[int]:
        if len(items) == 1:
            return self._send_single(items[0], group_key=key)
        return self._send_grouped(key, items)

    def _send_single(self, item: AlertItem, group_key: str) -> Optional[int]:
        pred = item.prediction
        severity = item.severity
        subject = (f"[СПА][{severity.value}] Предаварийное состояние: "
                   f"{pred.machine_id} ({item.machine_type}) — "
                   f"P={pred.failure_probability:.2f}, "
                   f"RUL~{pred.remaining_useful_life_days:.0f} дн.")
        ctx = {
            "machine_id": pred.machine_id,
            "machine_type": item.machine_type,
            "timestamp": pred.timestamp.strftime("%d.%m.%Y - %H:%M:%S UTC"),
            "probability": pred.failure_probability,
            "rul": pred.remaining_useful_life_days,
            "threshold": pred.threshold,
            "incident_id": item.incident_id,
            "severity_label": severity.label_ru,
            "severity_color": charts.SEVERITY_COLOR.get(severity.value, "#e6b800"),
            "actions": _ACTIONS_BY_SEVERITY.get(severity, _ACTIONS_BASE),
            "chart_cid": "spa-dynamics",
        }
        text_body = email_render.render_alert_text(ctx)
        html_body = email_render.render_alert_html(ctx)
        try:
            history = self.db.predictions_history(pred.machine_id, limit=200)
        except Exception:
            history = []
        png = charts.render_machine_dynamics(
            history, pred.threshold, pred.machine_id, item.machine_type)
        msg = self._build_message(
            subject, text_body, html_body, png,
            attach_name=f"dynamics_{pred.machine_id}.png",
            chart_cid="spa-dynamics", severity=severity,
        )
        return self._dispatch(
            msg, machine_id=pred.machine_id, incident_id=item.incident_id,
            severity=severity, group_key=group_key, grouped_count=1,
            event_time=item.event_time,
        )

    def _send_grouped(self, key: str, items: list) -> Optional[int]:
        severity = items[0].severity
        machine_type = items[0].machine_type
        machines = [{
            "machine_id": it.prediction.machine_id,
            "probability": it.prediction.failure_probability,
            "rul": it.prediction.remaining_useful_life_days,
            "incident_id": it.incident_id,
            "severity": it.severity.value,
        } for it in items]
        machines.sort(key=lambda m: m["probability"], reverse=True)
        subject = (f"[СПА][{severity.value}] Сводное оповещение: "
                   f"{len(items)}x{machine_type} — предаварийные состояния")
        ctx = {
            "machine_type": machine_type,
            "count": len(items),
            "group_key": key,
            "machines": machines,
            "severity_label": severity.label_ru,
            "severity_color": charts.SEVERITY_COLOR.get(severity.value, "#e6b800"),
            "actions": _ACTIONS_GROUP,
            "chart_cid": "spa-group",
        }
        text_body = email_render.render_group_text(ctx)
        html_body = email_render.render_group_html(ctx)
        png = charts.render_group_bar(machines)
        msg = self._build_message(
            subject, text_body, html_body, png,
            attach_name=f"group_{machine_type}_{severity.value}.png",
            chart_cid="spa-group", severity=severity,
        )
        # Состав письма пишется в журнал по записи на агрегат: у каждого
        # свой инцидент и своё время события (от него идёт окно подавления).
        by_machine: dict[str, AlertItem] = {}
        for it in items:
            known = by_machine.get(it.prediction.machine_id)
            if known is None or it.event_time >= known.event_time:
                by_machine[it.prediction.machine_id] = it
        members = [{
            "machine_id": mid, "incident_id": it.incident_id,
            "event_time": it.event_time,
        } for mid, it in sorted(by_machine.items())]
        representative = machines[0]["machine_id"]
        return self._dispatch(
            msg, machine_id=representative,
            incident_id=by_machine[representative].incident_id,
            severity=severity, group_key=key, grouped_count=len(items),
            event_time=by_machine[representative].event_time, members=members,
        )

    # ----- Формирование MIME-сообщения и фиксация в журнале -----
    def _build_message(self, subject: str, text_body: str, html_body: str,
                       png: bytes, attach_name: str, chart_cid: str,
                       severity: Severity) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="spa.local")
        msg["X-SPA-Severity"] = severity.value
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        html_part = msg.get_payload()[1]
        html_part.add_related(png, maintype="image", subtype="png",
                              cid=f"<{chart_cid}>")
        msg.add_attachment(png, maintype="image", subtype="png",
                           filename=attach_name)
        return msg

    def _dispatch(self, msg: EmailMessage, machine_id: str,
                  incident_id: Optional[int], severity: Severity,
                  group_key: str, grouped_count: int, event_time: datetime,
                  members: Optional[list[dict]] = None) -> Optional[int]:
        """Доставляет оповещение по всем каналам, включённым в настройках
        профиля (почта, SMS, push), и фиксирует результат каждой попытки
        в журнале оповещений (у сводного — вместе с составом группы).
        Возвращает идентификатор первой успешной записи либо None, если
        ни один канал не доставил сообщение."""
        cfg = self._channel_config()
        subject = str(msg["Subject"])
        plain = self._plain_of(msg)
        primary_id: Optional[int] = None

        def _record(channel: str, recipient: str, status: str,
                    error: Optional[str] = None) -> int:
            return self.db.insert_alert(
                incident_id=incident_id, machine_id=machine_id,
                sent_at=event_time, recipient=recipient or "—",
                subject=subject, body=plain, channel=channel, status=status,
                severity=severity.value, group_key=group_key,
                grouped_count=grouped_count, error=error, members=members,
            )

        # Канал «почта»: доставка multipart-сообщения штатным транспортом.
        if cfg.get("email_enabled"):
            recipient = cfg.get("email") or self.recipient
            if msg["To"] is None:
                msg["To"] = recipient
            else:
                msg.replace_header("To", recipient)
            try:
                self.transport.send(msg)
                primary_id = _record(self.mode, recipient, "sent")
            except Exception as exc:
                log.exception("Ошибка доставки оповещения (почта) по %s", machine_id)
                _record(self.mode, recipient, "failed", str(exc))

        # Канал «SMS»: демонстрационный транспорт (фиксация в журнале).
        if cfg.get("sms_enabled"):
            recipient = cfg.get("phone") or ""
            try:
                self.sms_transport.send(recipient, subject, plain)
                aid = _record("sms", recipient, "sent")
                primary_id = primary_id or aid
            except Exception as exc:
                log.exception("Ошибка доставки оповещения (SMS) по %s", machine_id)
                _record("sms", recipient, "failed", str(exc))

        # Канал «push»: демонстрационный транспорт (фиксация в журнале).
        if cfg.get("push_enabled"):
            recipient = cfg.get("email") or self.recipient
            try:
                self.push_transport.send(recipient, subject, plain)
                aid = _record("push", recipient, "sent")
                primary_id = primary_id or aid
            except Exception as exc:
                log.exception("Ошибка доставки оповещения (push) по %s", machine_id)
                _record("push", recipient, "failed", str(exc))

        return primary_id

    @staticmethod
    def _plain_of(msg: EmailMessage) -> str:
        try:
            part = msg.get_body(preferencelist=("plain",))
            return part.get_content() if part is not None else ""
        except Exception:
            return ""
