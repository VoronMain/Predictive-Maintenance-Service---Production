# -*- coding: utf-8 -*-
"""
forge_stream.py — непрерывный эмулятор телеметрии кузнечно-прессового цеха.

Генерирует измерения для 30 фиксированных агрегатов и направляет их
в эндпоинт /ingest монолитного приложения СПА. В отличие от emulator.py
не зависит от CSV-датасета: базовые значения сенсоров берутся из
профилей, определённых в app/forge_machines.py.

Запуск:
    python emulator/forge_stream.py
    python emulator/forge_stream.py --interval 60 --api-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# Подключение пакета app из директории СПА.
_SPA_DIR = Path(__file__).resolve().parent.parent
if str(_SPA_DIR) not in sys.path:
    sys.path.insert(0, str(_SPA_DIR))

from app.forge_machines import (  # noqa: E402
    FORGE_MACHINES,
    SEED_HISTORY_DAYS,
    SENSOR_PROFILES,
    generate_sensor_values,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] forge_stream: %(message)s",
)
log = logging.getLogger("forge_stream")

_STOP_REQUESTED = False


def _handle_stop(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log.info("Получен сигнал прерывания, завершение потока...")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _generate_payload(machine, rng: random.Random, t: float) -> dict:
    """Генерирует измерение, продолжающее траекторию деградации засева.

    Аргумент ``t`` — прогресс деградации: живой поток стартует с t ≈ 1.0
    (конец засеянного 14-дневного окна) и медленно растёт со временем, что
    исключает разрыв прогнозов на стыке истории и потока. Сенсорные значения
    берутся из общего источника app.forge_machines.generate_sensor_values.
    """
    profile = SENSOR_PROFILES[machine.state]
    sensors = generate_sensor_values(machine, t, rng)

    if machine.state == "pre_failure":
        last_maint = profile["last_maintenance_days_ago"] + rng.randint(-2, 2)
    else:
        last_maint = profile["last_maintenance_days_ago"] + rng.randint(-1, 1)

    return {
        "machine_id": machine.machine_id,
        "machine_type": machine.ml_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operational_hours": round(machine.operational_hours + rng.uniform(0, 1), 1),
        "temperature_c": sensors["temperature_c"],
        "vibration_mms": sensors["vibration_mms"],
        "sound_db": sensors["sound_db"],
        "oil_level_pct": sensors["oil_level_pct"],
        "coolant_level_pct": sensors["coolant_level_pct"],
        "power_consumption_kw": sensors["power_consumption_kw"],
        "last_maintenance_days_ago": int(_clamp(last_maint, 0, 365)),
        "maintenance_history_count": machine.maintenance_history_count,
        "failure_history_count": machine.failure_history_count,
        "ai_supervision": True,
        "error_codes_last_30_days": sensors["error_codes_last_30_days"],
        "ai_override_events": sensors["ai_override_events"],
        "laser_intensity": None,
        "hydraulic_pressure_bar": None,
        "coolant_flow_l_min": None,
        "heat_index": None,
    }


def wait_for_server(url: str, timeout: int) -> bool:
    """Поллинг /health до получения 200 или истечения timeout секунд."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{url}/health", timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def run_stream(api_url: str, auth: HTTPBasicAuth, cycle_seconds: float) -> None:
    """Бесконечный цикл отправки измерений для всех 30 агрегатов."""
    session = requests.Session()
    n = len(FORGE_MACHINES)
    per_machine_delay = cycle_seconds / n
    rngs = {m.machine_id: random.Random(abs(hash(m.machine_id)) % (2 ** 32))
            for m in FORGE_MACHINES}
    stats = {"sent": 0, "processed": 0, "buffered": 0, "failed": 0}
    # Момент старта потока ≈ «сейчас» сидера: от него продолжается
    # траектория деградации (t стартует с 1.0 — конца засеянного окна).
    stream_start = datetime.now(timezone.utc)
    log.info("Поток запущен: %d агрегатов, цикл %.0f с", n, cycle_seconds)

    while not _STOP_REQUESTED:
        elapsed_days = (datetime.now(timezone.utc)
                        - stream_start).total_seconds() / 86400.0
        for machine in FORGE_MACHINES:
            if _STOP_REQUESTED:
                break
            window = min(SEED_HISTORY_DAYS, machine.history_days)
            t = 1.0 + elapsed_days / max(window, 1)
            payload = _generate_payload(machine, rngs[machine.machine_id], t)
            try:
                resp = session.post(f"{api_url}/ingest",
                                    json=payload, auth=auth, timeout=10)
                stats["sent"] += 1
                if resp.status_code == 200:
                    stats["processed"] += 1
                    pred = resp.json().get("prediction", {})
                    log.debug("+ %s p=%.3f RUL=%.1f",
                              machine.machine_id,
                              pred.get("failure_probability", 0),
                              pred.get("remaining_useful_life_days", 0))
                elif resp.status_code == 202:
                    stats["buffered"] += 1
                else:
                    stats["failed"] += 1
                    log.warning("HTTP %d для %s", resp.status_code, machine.machine_id)
            except requests.RequestException as exc:
                stats["failed"] += 1
                log.error("Ошибка сети (%s): %s", machine.machine_id, exc)
            if per_machine_delay > 0:
                time.sleep(per_machine_delay)

        if stats["sent"] % (n * 10) == 0 and stats["sent"] > 0:
            log.info("Статистика: отправлено=%d обработано=%d буф=%d ошибок=%d",
                     stats["sent"], stats["processed"],
                     stats["buffered"], stats["failed"])


def main() -> int:
    load_dotenv(_SPA_DIR / ".env")

    parser = argparse.ArgumentParser(
        description="Непрерывный эмулятор телеметрии кузнечно-прессового цеха"
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("SPA_API_URL", "http://127.0.0.1:8000"),
        help="URL сервиса СПА (переменная SPA_API_URL)",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("SPA_BASIC_AUTH_USER", "admin"),
        help="Имя пользователя HTTP Basic (переменная SPA_BASIC_AUTH_USER)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("SPA_BASIC_AUTH_PASSWORD", "admin"),
        help="Пароль HTTP Basic (переменная SPA_BASIC_AUTH_PASSWORD)",
    )
    parser.add_argument(
        "--interval", type=float,
        default=float(os.getenv("SPA_EMULATOR_INTERVAL", "120")),
        help="Длительность одного цикла по всем агрегатам, с (переменная SPA_EMULATOR_INTERVAL)",
    )
    parser.add_argument(
        "--startup-timeout", type=int,
        default=int(os.getenv("SPA_EMULATOR_STARTUP_TIMEOUT", "120")),
        help="Максимальное время ожидания готовности сервера, с (0 — не ждать)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    if args.startup_timeout > 0:
        log.info("Ожидание готовности сервера %s (макс. %d с)...",
                 args.api_url, args.startup_timeout)
        if not wait_for_server(args.api_url, args.startup_timeout):
            log.error("Сервер не вышел на готовность за %d с. Остановка.",
                      args.startup_timeout)
            return 1
        log.info("Сервер готов.")

    auth = HTTPBasicAuth(args.user, args.password)
    log.info("Запуск -> %s, цикл %.0f с (%.1f с/агрегат)",
             args.api_url, args.interval, args.interval / len(FORGE_MACHINES))
    run_stream(args.api_url, auth, args.interval)
    log.info("Поток остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
