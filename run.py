# -*- coding: utf-8 -*-
"""run.py - управляющий скрипт системы предиктивной аналитики (СПА).

Запускает в одном сеансе HTTP-сервер FastAPI (через Uvicorn) и
эмулятор источника телеметрии. Поддерживает однократный и
непрерывный режим работы эмулятора.

Запуск:
    python run.py                       # однократная подача 60 измерений
    python run.py --continuous          # непрерывный поток данных
    python run.py --no-emulator         # только сервер
    python run.py --n-machines 50 --n-records 5 --interval 0.5
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] launcher: %(message)s",
)
log = logging.getLogger("launcher")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Управляющий скрипт системы предиктивной аналитики (СПА)"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Адрес интерфейса HTTP-сервера")
    parser.add_argument("--port", type=int, default=8000,
                        help="Порт HTTP-сервера")
    parser.add_argument("--no-emulator", action="store_true",
                        help="Не запускать эмулятор источника телеметрии")
    parser.add_argument("--stream-interval", type=float, default=120.0,
                        help="Длительность одного цикла по всем агрегатам, с (по умолч. 120)")
    parser.add_argument("--user",
                        default=os.getenv("SPA_BASIC_AUTH_USER", "admin"),
                        help="Имя пользователя HTTP Basic")
    parser.add_argument("--password",
                        default=os.getenv("SPA_BASIC_AUTH_PASSWORD", "admin"),
                        help="Пароль HTTP Basic")
    parser.add_argument("--startup-timeout", type=int, default=300,
                        help="Максимальное время ожидания готовности сервера, с")
    parser.add_argument("--continuous", action="store_true",
                        help=("Непрерывный режим: forge_stream.py поддерживает "
                              "постоянный поток измерений для 30 агрегатов."))
    return parser.parse_args()


def wait_for_server(base_url: str, timeout: int) -> bool:
    """Опрашивает /health до получения положительного ответа."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def main() -> int:
    args = parse_args()
    base_url = f"http://{args.host}:{args.port}"

    log.info("=" * 60)
    log.info("Запуск системы предиктивной аналитики (СПА)")
    log.info("=" * 60)
    log.info("HTTP-сервер: %s", base_url)
    log.info("Веб-интерфейс: %s/", base_url)
    log.info("Журнал отказов: %s/incidents-ui", base_url)
    log.info("Документация API: %s/docs", base_url)

    # Запуск Uvicorn в дочернем процессе.
    server_cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", args.host, "--port", str(args.port),
    ]
    log.info("Команда запуска сервера: %s", " ".join(server_cmd))
    server_proc = subprocess.Popen(server_cmd, cwd=str(ROOT))
    emulator_proc = None

    try:
        log.info("Ожидание готовности HTTP-сервера (макс. %d с)...",
                 args.startup_timeout)
        if not wait_for_server(base_url, args.startup_timeout):
            log.error("HTTP-сервер не вышел на готовность в отведённое время.")
            server_proc.terminate()
            return 1
        log.info("HTTP-сервер готов к приёму данных.")

        # Запуск эмулятора непрерывного потока (опционально).
        if not args.no_emulator:
            emulator_cmd = [
                sys.executable, str(ROOT / "emulator" / "forge_stream.py"),
                "--api-url", base_url,
                "--user", args.user,
                "--password", args.password,
                "--interval", str(args.stream_interval),
            ]
            log.info("Запуск forge_stream: %s", " ".join(emulator_cmd))
            emulator_proc = subprocess.Popen(emulator_cmd, cwd=str(ROOT))
        else:
            log.info("Эмулятор отключён флагом --no-emulator.")

        log.info("Система запущена. Для остановки нажмите Ctrl+C.")
        if emulator_proc is not None:
            ret = emulator_proc.wait()
            log.info("Эмулятор завершил работу с кодом %d.", ret)
            if not args.continuous:
                log.info(
                    "HTTP-сервер продолжает работу. Откройте %s/ "
                    "для просмотра дашборда. Для остановки нажмите Ctrl+C.",
                    base_url,
                )
        server_proc.wait()
        return 0

    except KeyboardInterrupt:
        log.info("")
        log.info("Получен сигнал прерывания, остановка подсистем...")
        return 0
    finally:
        for proc, name in [
            (emulator_proc, "эмулятор"),
            (server_proc, "HTTP-сервер"),
        ]:
            if proc is not None and proc.poll() is None:
                log.info("Остановка процесса: %s", name)
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.warning("Принудительное завершение %s", name)
                    proc.kill()


if __name__ == "__main__":
    sys.exit(main())
