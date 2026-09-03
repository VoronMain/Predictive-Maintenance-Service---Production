# -*- coding: utf-8 -*-
"""start.py — лончер контейнера СПА.

Поднимает в одном процессе-родителе два дочерних процесса:
  * uvicorn (app.main:app)            — HTTP-сервис;
  * emulator/forge_stream.py          — эмулятор потока телеметрии.

Конфигурируется только переменными окружения, без argparse-интерфейса —
рассчитан на запуск платформой (Railway и т. п.) как единственная команда
контейнера (`CMD ["python", "start.py"]` в exec-форме, чтобы этот процесс
был PID 1 и получал сигналы напрямую от Docker). Локальный сценарий
разработки — run.py в корне репозитория, он не меняется и продолжает
работать как раньше.

Порт HTTP-сервиса берётся из переменной PORT (её подставляет платформа при
деплое), при отсутствии — 8000, как и раньше при локальном запуске.

Эмулятору не передаются аргументы командной строки — он сам читает
SPA_API_URL, SPA_BASIC_AUTH_USER/PASSWORD, SPA_EMULATOR_INTERVAL,
SPA_EMULATOR_STARTUP_TIMEOUT из окружения (см. emulator/forge_stream.py).
Лончер лишь подставляет ему в окружение SPA_API_URL, указывающий на
локальный адрес HTTP-сервиса (http://127.0.0.1:$PORT), — снаружи он
неизвестен и не нужен, оба процесса живут в одном контейнере.

Остановка и обработка сбоев:
  * PID 1 в Linux-контейнере не получает сигналы для дочерних процессов
    автоматически, поэтому по SIGTERM/SIGINT лончер сам транслирует сигнал
    обоим дочерним процессам и дожидается их завершения (не дольше
    _STOP_TIMEOUT_SECONDS на процесс, иначе — SIGKILL). Это даёт uvicorn
    штатно отработать lifespan-остановку приложения, а эмулятору —
    завершить текущую итерацию цикла (см. _handle_stop в forge_stream.py).
    После штатной остановки лончер завершается с кодом 0.
  * Если один из дочерних процессов падает сам по себе (без внешнего
    сигнала), лончер останавливает второй процесс и завершается с
    ненулевым кодом — пусть платформа перезапустит контейнер по
    restartPolicy (см. railway.json, ON_FAILURE).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] start: %(message)s",
)
log = logging.getLogger("start")

_POLL_INTERVAL_SECONDS = 1.0
_STOP_TIMEOUT_SECONDS = 10.0

_STOP_REQUESTED = False


def _handle_stop(signum, frame) -> None:  # noqa: D401 - обработчик сигнала
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log.info("Получен сигнал %s, останавливаю дочерние процессы...", signum)


def _stop_process(proc: subprocess.Popen, name: str) -> None:
    """Останавливает процесс SIGTERM'ом, при таймауте — SIGKILL."""
    if proc.poll() is not None:
        return
    log.info("Остановка процесса: %s (pid=%s)", name, proc.pid)
    try:
        proc.terminate()  # на Linux — SIGTERM
    except Exception:
        log.exception("Не удалось отправить SIGTERM процессу %s", name)
    try:
        proc.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning(
            "Процесс %s не остановился за %.0f с, отправляю SIGKILL",
            name, _STOP_TIMEOUT_SECONDS,
        )
        proc.kill()
        proc.wait()


def main() -> int:
    port = os.environ.get("PORT", "8000")
    base_url = f"http://127.0.0.1:{port}"

    log.info("=" * 60)
    log.info("Запуск контейнера СПА (start.py)")
    log.info("=" * 60)

    server_cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", str(port),
    ]
    log.info("HTTP-сервис: %s", " ".join(server_cmd))
    server_proc = subprocess.Popen(server_cmd, cwd=str(ROOT))

    emulator_env = dict(os.environ)
    emulator_env["SPA_API_URL"] = base_url
    emulator_cmd = [sys.executable, str(ROOT / "emulator" / "forge_stream.py")]
    log.info("Эмулятор телеметрии: %s (SPA_API_URL=%s)", " ".join(emulator_cmd), base_url)
    emulator_proc = subprocess.Popen(emulator_cmd, cwd=str(ROOT), env=emulator_env)

    # Регистрируем обработчик после старта дочерних процессов, чтобы окно
    # между их запуском и установкой обработчика было минимальным — если
    # сигнал придёт раньше (крайне маловероятно), процессы всё равно
    # завершатся вместе с родителем при получении Docker'ом SIGKILL по
    # истечении grace-периода.
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # Порядок для мониторинга (не важен), и отдельный порядок для остановки:
    # при штатном SIGTERM эмулятор гасим первым, чтобы он не успел прислать
    # в /ingest запрос после того, как HTTP-сервер уже завершил приём —
    # это лишь убирает единичный безвредный, но шумный "Connection refused"
    # в логе эмулятора перед его собственной остановкой.
    procs = {"HTTP-сервер": server_proc, "эмулятор": emulator_proc}
    stop_order = [
        ("эмулятор", emulator_proc),
        ("HTTP-сервер", server_proc),
    ]
    crashed_name: str | None = None
    crashed_code: int | None = None

    try:
        while not _STOP_REQUESTED:
            for name, proc in procs.items():
                ret = proc.poll()
                if ret is not None:
                    crashed_name, crashed_code = name, ret
                    break
            if crashed_name is not None:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        for name, proc in stop_order:
            _stop_process(proc, name)

    if crashed_name is not None:
        log.error(
            "Процесс %s неожиданно завершился с кодом %s — контейнер "
            "останавливается с ненулевым кодом.", crashed_name, crashed_code,
        )
        return crashed_code if crashed_code not in (None, 0) else 1

    log.info("Контейнер остановлен штатно (SIGTERM/SIGINT).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
