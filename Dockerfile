# syntax=docker/dockerfile:1
# Образ СПА: в одном контейнере HTTP-сервис (uvicorn) и поток телеметрии
# (emulator/forge_stream.py), поднимаемые лончером start.py.
FROM python:3.13-slim

# libgomp1 — рантайм OpenMP, нужен LightGBM и CatBoost для инференса.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости ставим отдельным слоем до копирования кода — пересборка
# образа при правках кода не переустанавливает пакеты.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения, эмулятора и пакет инференса вместе с артефактами
# моделей (predictive_maintenance/models/). Датасет и тренировочные
# артефакты (Датасет/, Модели/artifacts/, catboost_info/ и т. п.) в
# рантайме не читаются и в образ не входят — их и нет в дереве
# репозитория, см. .dockerignore для дублирующей защиты.
COPY app/ ./app/
COPY emulator/ ./emulator/
COPY predictive_maintenance/ ./predictive_maintenance/
COPY start.py ./start.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Exec-форма: start.py становится PID 1 контейнера и получает SIGTERM/
# SIGINT от Docker напрямую (важно для штатной остановки — см. start.py).
CMD ["python", "start.py"]
