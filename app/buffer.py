# -*- coding: utf-8 -*-
"""
buffer.py — подсистема буферизации и агрегации потоковых измерений.

Принятые измерения помещаются в кольцевой буфер оперативной памяти,
организованный отдельно для каждой единицы оборудования. По истечении
временного окна агрегации (по умолчанию один час) вычисляются
статистические показатели потока: скользящие средние, максимумы,
минимумы, среднеквадратические отклонения и иные характеристики.
"""
from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, Optional

from .schema import TelemetryMeasurement


# Перечень числовых полей, по которым вычисляются агрегированные
# характеристики потока в пределах временного окна.
NUMERIC_FIELDS = (
    "temperature_c",
    "vibration_mms",
    "sound_db",
    "oil_level_pct",
    "coolant_level_pct",
    "power_consumption_kw",
    "operational_hours",
    "last_maintenance_days_ago",
    "error_codes_last_30_days",
    "ai_override_events",
    "laser_intensity",
    "hydraulic_pressure_bar",
    "coolant_flow_l_min",
    "heat_index",
)


def _safe_stats(values: list[float]) -> dict:
    """Вычисляет базовые статистики по непустому списку значений.

    При пустом списке возвращает словарь с NaN, что соответствует
    требованию архитектурного документа: значения признаков MNAR
    при отсутствии измерений сохраняются как NaN.
    """
    if not values:
        return {"mean": float("nan"), "min": float("nan"),
                "max": float("nan"), "std": float("nan")}
    mean = statistics.fmean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    return {"mean": mean, "min": min(values), "max": max(values), "std": std}


class CircularBuffer:
    """Кольцевой буфер измерений для одной единицы оборудования.

    Хранит измерения, попадающие в текущее временное окно. По мере
    поступления новых измерений устаревшие записи вытесняются.
    """

    def __init__(self, window_seconds: int) -> None:
        self.window = timedelta(seconds=window_seconds)
        self._items: Deque[TelemetryMeasurement] = deque()
        self._lock = threading.RLock()

    def append(self, measurement: TelemetryMeasurement) -> None:
        with self._lock:
            self._items.append(measurement)
            cutoff = measurement.timestamp - self.window
            while self._items and self._items[0].timestamp < cutoff:
                self._items.popleft()

    def snapshot(self) -> list[TelemetryMeasurement]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class AggregationManager:
    """Управление набором буферов и формирование агрегатов по окну.

    Реализует одинаковую логику для всех единиц оборудования. Метод
    aggregate выполняет расчёт сводных характеристик по содержимому
    буфера и возвращает агрегированный вектор измерений.
    """

    def __init__(self, window_seconds: int) -> None:
        self.window_seconds = window_seconds
        self._buffers: Dict[str, CircularBuffer] = {}
        self._lock = threading.RLock()

    def _get_or_create(self, machine_id: str) -> CircularBuffer:
        with self._lock:
            buf = self._buffers.get(machine_id)
            if buf is None:
                buf = CircularBuffer(self.window_seconds)
                self._buffers[machine_id] = buf
            return buf

    def append(self, measurement: TelemetryMeasurement) -> None:
        self._get_or_create(measurement.machine_id).append(measurement)

    def buffered_machines(self) -> list[str]:
        with self._lock:
            return list(self._buffers.keys())

    def aggregate(self, machine_id: str) -> Optional[dict]:
        """Формирует агрегированный вектор по буферу единицы оборудования.

        Возвращает None, если буфер пуст. Иначе возвращает словарь,
        содержащий сводные статистики и метаданные последнего
        измерения, необходимые для последующего формирования
        вектора признаков ML-инференса.
        """
        buf = self._buffers.get(machine_id)
        if buf is None:
            return None
        items = buf.snapshot()
        if not items:
            return None
        # Последнее измерение используется как источник статических
        # признаков (тип оборудования, год установки и др.).
        last = items[-1]
        result: dict = {
            "machine_id": machine_id,
            "machine_type": last.machine_type,
            "ai_supervision": int(last.ai_supervision),
            "maintenance_history_count": last.maintenance_history_count,
            "failure_history_count": last.failure_history_count,
            "window_end": last.timestamp,
            "n_samples": len(items),
        }
        for field in NUMERIC_FIELDS:
            values = [getattr(m, field) for m in items if getattr(m, field) is not None
                      and not (isinstance(getattr(m, field), float)
                               and math.isnan(getattr(m, field)))]
            stats = _safe_stats(values)
            for k, v in stats.items():
                result[f"{field}_{k}"] = v
        return result
