# -*- coding: utf-8 -*-
"""
main.py — точка входа в монолитное приложение СПА на базе FastAPI.

Приложение объединяет в одном процессе подсистемы сбора, валидации,
буферизации, формирования признаков, ML-инференса и хранения данных.
Доступ к REST API защищён штатным механизмом HTTP Basic Authentication.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .buffer import AggregationManager
from .config import settings
from .database import create_database
from .forge_machines import FORGE_MACHINES, SEED_HISTORY_DAYS
from .incidents import IncidentDetector
from .ml_service import MLService
from .notifications import NotificationService
from .pipeline import Pipeline
from .schema import EquipmentRecord, TelemetryMeasurement
from .seeder import is_already_seeded, seed_historical_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("spa")

security = HTTPBasic()


def authenticate(credentials: Annotated[HTTPBasicCredentials, Depends(security)]):
    correct_user = secrets.compare_digest(
        credentials.username, settings.BASIC_AUTH_USER
    )
    correct_pass = secrets.compare_digest(
        credentials.password, settings.BASIC_AUTH_PASSWORD
    )
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учётные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация подсистем при старте приложения и корректное
    освобождение ресурсов при остановке."""
    db = create_database()

    # Синхронизация справочника агрегатов (категории, годы установки).
    for _m in FORGE_MACHINES:
        db.upsert_equipment(EquipmentRecord(
            machine_id=_m.machine_id,
            machine_type=_m.ml_type,
            operational_hours=_m.operational_hours,
            category=_m.category,
        ))

    log.info("Инициализация ML-сервиса (порог t*=%.2f)", settings.FAILURE_THRESHOLD)
    ml = MLService.from_path(
        settings.MODELS_DIR, threshold=settings.FAILURE_THRESHOLD
    )

    # Засев выполняется тем же ML-сервисом, что и живой поток, поэтому
    # инициализируется после загрузки моделей. Это исключает разрыв
    # прогнозов на стыке исторических и потоковых данных.
    if not is_already_seeded(db):
        log.info("Засев исторических данных (%d дн.) для %d агрегатов...",
                 SEED_HISTORY_DAYS, len(FORGE_MACHINES))
        await asyncio.to_thread(seed_historical_data, db, ml, FORGE_MACHINES,
                                days=SEED_HISTORY_DAYS)
    else:
        log.info("Засев пропущен — исторические данные уже присутствуют в БД.")

    aggregator = AggregationManager(window_seconds=settings.AGGREGATION_WINDOW_SECONDS)

    log.info("Инициализация подсистемы оповещений (режим=%s)",
             settings.SMTP_MODE)
    incidents = IncidentDetector(db=db)
    notifier = NotificationService.from_settings(db=db)

    pipeline = Pipeline(
        db=db,
        aggregator=aggregator,
        ml_service=ml,
        incident_detector=incidents,
        notifier=notifier,
    )

    app.state.db = db
    app.state.ml = ml
    app.state.aggregator = aggregator
    app.state.incidents = incidents
    app.state.notifier = notifier
    app.state.pipeline = pipeline

    # Фоновый планировщик сброса групп оповещений: по истечении окна
    # агрегации накопленные однотипные оповещения доставляются даже при
    # отсутствии новых поступлений.
    async def _alert_flusher() -> None:
        interval = max(1, settings.ALERT_GROUP_WINDOW_SECONDS // 2 or 5)
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(notifier.flush_due)
            except Exception:
                log.exception("Ошибка планового сброса групп оповещений")

    flush_task = asyncio.create_task(_alert_flusher())

    log.info("Приложение СПА готово к приёму данных.")
    try:
        yield
    finally:
        flush_task.cancel()
        try:
            notifier.flush_all()
        except Exception:
            log.exception("Ошибка финального сброса групп оповещений")
        db.close()
        log.info("Приложение СПА остановлено.")


app = FastAPI(
    title="СПА — система предиктивной аналитики",
    version="0.2.0",
    description=(
        "Монолитное приложение мониторинга технического состояния "
        "промышленного оборудования. Объединяет конвейер сбора, "
        "обработки и ML-инференса (LightGBM + CatBoost)."
    ),
    lifespan=lifespan,
)

_STATIC_DIR = Path(__file__).parent / "static"

app.mount(
    "/static",
    StaticFiles(directory=str(_STATIC_DIR)),
    name="static",
)

# Нормативные диапазоны датчиков [нижний, верхний] — единый источник
# для трёхцветной индикации в веб-интерфейсе. Страницы «Цех» и карточка
# агрегата получают их через эндпоинт /config.
SENSOR_LIMITS: dict[str, list[float]] = {
    "temperature_c":        [10, 90],
    "vibration_mms":        [0, 20],
    "sound_db":             [75, 110],
    "oil_level_pct":        [30, 100],
    "coolant_level_pct":    [25, 100],
    "power_consumption_kw": [50, 500],
}


class NotificationSettingsIn(BaseModel):
    """Тело запроса на сохранение настроек оповещений профиля."""

    email_enabled: bool = True
    sms_enabled: bool = False
    push_enabled: bool = False
    email: str = ""
    phone: str = ""
    failure_threshold: float = Field(
        default=settings.FAILURE_THRESHOLD, ge=0.0, le=1.0
    )


# --------------------------------------------------------------- #
# Служебные эндпоинты
# --------------------------------------------------------------- #
@app.get("/health", tags=["service"])
def health() -> dict:
    """Проверка работоспособности приложения."""
    return {"status": "ok"}


@app.get("/info", tags=["service"])
def info(user: Annotated[str, Depends(authenticate)]) -> dict:
    """Сводная информация о загруженной ML-модели."""
    return app.state.ml.info()


# --------------------------------------------------------------- #
# Приём потоковой телеметрии
# --------------------------------------------------------------- #
@app.post("/ingest", tags=["ingestion"])
def ingest_measurement(
    measurement: TelemetryMeasurement,
    user: Annotated[str, Depends(authenticate)],
) -> JSONResponse:
    """Принимает одно измерение от эмулятора источника телеметрии."""
    prediction = app.state.pipeline.ingest(measurement)
    if prediction is None:
        return JSONResponse(
            content={"status": "buffered", "machine_id": measurement.machine_id},
            status_code=202,
        )
    return JSONResponse(
        content={
            "status": "processed",
            "prediction": prediction.model_dump(mode="json"),
        },
        status_code=200,
    )


# --------------------------------------------------------------- #
# Чтение результатов
# --------------------------------------------------------------- #
@app.get("/equipment", tags=["query"])
def list_equipment(user: Annotated[str, Depends(authenticate)]) -> list[dict]:
    """Возвращает справочник зарегистрированного оборудования."""
    return app.state.db.list_equipment()


@app.get("/predictions/latest", tags=["query"])
def latest_predictions(
    user: Annotated[str, Depends(authenticate)],
    limit: int = 100,
) -> list[dict]:
    """Возвращает последние предсказания, отсортированные по убыванию времени."""
    return app.state.db.latest_predictions(limit=limit)


@app.get("/predictions/overview", tags=["query"])
def predictions_overview(user: Annotated[str, Depends(authenticate)]) -> list[dict]:
    """Все агрегаты с последним предсказанием (NULL у машин без предсказаний)."""
    return app.state.db.all_machines_overview()


@app.get("/dashboard/stats", tags=["query"])
def dashboard_stats(user: Annotated[str, Depends(authenticate)]) -> dict:
    """Агрегированные показатели для дашборда: парк, статусы, категории, инциденты."""
    db = app.state.db
    threshold = settings.FAILURE_THRESHOLD

    equipment = db.list_equipment()
    preds = db.all_machines_overview()
    pred_by_id = {p["machine_id"]: p for p in preds if p.get("failure_probability") is not None}

    by_category: dict[str, int] = {}
    pre_failure = 0
    normal_count = 0
    new_count = 0

    for eq in equipment:
        cat = eq.get("category") or "Прочее"
        by_category[cat] = by_category.get(cat, 0) + 1

        if eq["operational_hours"] < 1000.0:
            new_count += 1
        elif eq["machine_id"] in pred_by_id:
            p = pred_by_id[eq["machine_id"]]
            if p["failure_probability"] >= threshold:
                pre_failure += 1
            else:
                normal_count += 1
        else:
            new_count += 1

    summary = db.incidents_summary()
    alerts_sent = db.get_alerts_count()

    return {
        "total": len(equipment),
        "by_category": by_category,
        "by_status": {
            "pre_failure": pre_failure,
            "normal": normal_count,
            "new": new_count,
        },
        "incidents_open": summary["open"],
        "incidents_total": summary["total"],
        "alerts_sent": alerts_sent,
    }


@app.get("/predictions/{machine_id}", tags=["query"])
def predictions_history(
    machine_id: str,
    user: Annotated[str, Depends(authenticate)],
    limit: int = 200,
) -> list[dict]:
    """История предсказаний по конкретной единице оборудования."""
    return app.state.db.predictions_history(machine_id, limit=limit)


@app.get("/telemetry/{machine_id}/sensor-averages", tags=["query"])
def telemetry_sensor_averages(
    machine_id: str,
    user: Annotated[str, Depends(authenticate)],
    days: int = 7,
) -> dict:
    """Средние значения шести сенсоров за последние N дней."""
    return app.state.db.get_sensor_averages(machine_id, days=days)


@app.get("/telemetry/{machine_id}", tags=["query"])
def telemetry_history(
    machine_id: str,
    user: Annotated[str, Depends(authenticate)],
    limit: int = 50,
) -> list[dict]:
    """Последние сырые измерения по единице оборудования."""
    return app.state.db.latest_raw_measurements(machine_id, limit=limit)


# --------------------------------------------------------------- #
# Журнал отказов и оповещений
# --------------------------------------------------------------- #
@app.get("/incidents", tags=["incidents"])
def incidents_list(
    user: Annotated[str, Depends(authenticate)],
    status: str | None = None,
    machine_type: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Возвращает записи электронного журнала отказов."""
    return app.state.db.list_incidents(
        status=status, machine_type=machine_type, limit=limit
    )


@app.get("/incidents/summary", tags=["incidents"])
def incidents_summary(user: Annotated[str, Depends(authenticate)]) -> dict:
    """Возвращает агрегированные показатели по журналу отказов."""
    return app.state.db.incidents_summary()


@app.get("/alerts", tags=["incidents"])
def alerts_list(
    user: Annotated[str, Depends(authenticate)],
    limit: int = 100,
) -> list[dict]:
    """Возвращает записи журнала отправленных оповещений."""
    return app.state.db.list_alerts(limit=limit)


@app.get("/alerts/{alert_id}", tags=["incidents"])
def alert_detail(
    alert_id: int,
    user: Annotated[str, Depends(authenticate)],
) -> dict:
    """Возвращает полные сведения об оповещении, включая текст сообщения."""
    alert = app.state.db.get_alert(alert_id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Оповещение не найдено",
        )
    return alert


_MACHINE_DISPLAY_NAMES = {m.machine_id: m.display_name for m in FORGE_MACHINES}


# --------------------------------------------------------------- #
# Конфигурация и метаданные оборудования для веб-интерфейса
# --------------------------------------------------------------- #
@app.get("/config", tags=["query"])
def ui_config(user: Annotated[str, Depends(authenticate)]) -> dict:
    """Параметры отображения для веб-интерфейса: порог классификации t*
    и нормативные диапазоны датчиков (единый источник трёхцветной
    индикации на страницах «Цех» и карточки агрегата)."""
    return {"threshold": settings.FAILURE_THRESHOLD, "limits": SENSOR_LIMITS}


@app.get("/equipment/{machine_id}", tags=["query"])
def equipment_detail(
    machine_id: str,
    user: Annotated[str, Depends(authenticate)],
) -> dict:
    """Метаданные единицы оборудования (наименование, тип, категория,
    наработка) для карточки агрегата."""
    eq_list = app.state.db.list_equipment()
    eq = next((e for e in eq_list if e["machine_id"] == machine_id), None)
    return {
        "machine_id": machine_id,
        "display_name": _MACHINE_DISPLAY_NAMES.get(machine_id, machine_id),
        "machine_type": (eq or {}).get("machine_type", ""),
        "category": (eq or {}).get("category", ""),
        "operational_hours": (eq or {}).get("operational_hours", 0),
    }


# --------------------------------------------------------------- #
# Настройки оповещений (профиль инспектора БППР)
# --------------------------------------------------------------- #
@app.get("/settings/notifications", tags=["settings"])
def read_notification_settings(
    user: Annotated[str, Depends(authenticate)],
) -> dict:
    """Текущие настройки автоматических оповещений."""
    return app.state.db.get_notification_settings()


@app.put("/settings/notifications", tags=["settings"])
def update_notification_settings(
    payload: NotificationSettingsIn,
    user: Annotated[str, Depends(authenticate)],
) -> dict:
    """Сохраняет настройки оповещений (каналы доставки, адресаты, порог)."""
    return app.state.db.save_notification_settings(
        email_enabled=payload.email_enabled,
        sms_enabled=payload.sms_enabled,
        push_enabled=payload.push_enabled,
        email=payload.email,
        phone=payload.phone,
        failure_threshold=payload.failure_threshold,
    )


# --------------------------------------------------------------- #
# Веб-интерфейс (статические страницы HTML + Tailwind)
# --------------------------------------------------------------- #
@app.get("/", tags=["ui"])
def overview_page() -> FileResponse:
    """Страница общего мониторинга цеха (Overview)."""
    return FileResponse(_STATIC_DIR / "overview.html")


@app.get("/machine/{machine_id}", tags=["ui"])
def machine_detail_page(machine_id: str) -> FileResponse:
    """Карточка агрегата (Drill-down). Идентификатор считывается
    клиентским кодом из пути запроса."""
    return FileResponse(_STATIC_DIR / "machine.html")


@app.get("/incidents-ui", tags=["ui"])
def incidents_page() -> FileResponse:
    """Страница электронного журнала отказов."""
    return FileResponse(_STATIC_DIR / "incidents.html")


@app.get("/settings", tags=["ui"])
def settings_page() -> FileResponse:
    """Страница настроек оповещений (профиль)."""
    return FileResponse(_STATIC_DIR / "settings.html")
