# -*- coding: utf-8 -*-
"""
example.py — пример использования пакета predictive_maintenance
для интеграции моделей в производственную систему.

Демонстрирует:
  * загрузку обученной модели из директории моделей;
  * прогноз бинарной метки 'Failure_Within_7_Days' с калибровкой
    и оптимальным порогом классификации;
  * прогноз остаточного ресурса оборудования (RUL) в сутках;
  * получение сводной информации о модели.

Запуск:
    python -m predictive_maintenance.example <путь_к_parquet>
или:
    python predictive_maintenance/example.py <путь_к_parquet>
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# Запуск как самостоятельного скрипта: добавляем родительскую папку в sys.path
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from predictive_maintenance import PredictiveMaintenanceModel


MODEL_DIR = Path(__file__).resolve().parent / "models"


def main(data_path: str | None = None) -> None:
    print(f"Загрузка модели из {MODEL_DIR} ...")
    model = PredictiveMaintenanceModel.load(MODEL_DIR)
    print("Модель загружена. Сводка:")
    for k, v in model.info().items():
        print(f"  {k}: {v}")

    # Берём небольшую тестовую выборку, если путь не указан
    if data_path is None:
        default_test = MODEL_DIR.parent.parent.parent / "Датасет" / "X_clf_test.parquet"
        if default_test.exists():
            data_path = str(default_test)
            print(f"\nИспользуется тестовый файл по умолчанию: {data_path}")
        else:
            print("\nУкажите путь к parquet-файлу с признаками.")
            sys.exit(1)

    df = pd.read_parquet(data_path).head(10)
    print(f"\nЗагружено {len(df)} наблюдений; столбцов: {len(df.columns)}.")

    # Прогноз отказа
    failure = model.predict_failure(df)
    print(f"\nПрогноз отказа (порог t* = {failure.threshold:.2f}):")
    print(f"  Вероятности: {failure.probability.round(4).tolist()}")
    print(f"  Метки:       {failure.label.tolist()}")

    # Прогноз RUL
    rul = model.predict_rul(df)
    print(f"\nПрогноз RUL (контрольный RMSE = {rul.rmse_holdout:.2f} дн.):")
    print(f"  RUL, дн.: {rul.rul_days.round(1).tolist()}")

    # Совмещённый результат
    result = model.predict(df)
    print("\nСовмещённый прогноз (predict):")
    print(result.to_string(index=False, max_rows=10))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(arg)
