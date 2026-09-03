# predictive_maintenance

Пакет инференса моделей предиктивного обслуживания промышленного
оборудования. Реализует две связанных задачи:

| Задача | Алгоритм | Метрика | Значение на тесте |
|--------|----------|---------|-------------------|
| Прогноз отказа в 7 суток (бинарная) | LightGBM + IsotonicCalibration + Threshold Moving | F1 | 0,7147 |
| Прогноз остаточного ресурса, дн. | CatBoost (`nan_mode='Min'`, RMSE) | RMSE | 48,35 дн. |

## Структура

```
predictive_maintenance/
├── __init__.py            Экспорт публичного API
├── inference.py           Класс PredictiveMaintenanceModel
├── preprocessing.py       Восстановление NaN для MNAR-признаков
├── schema.py              Перечень 67 признаков, валидация схемы
├── example.py             Пример использования
├── requirements.txt       Версии зависимостей
└── models/
    ├── lightgbm_classifier.joblib       Основной классификатор
    ├── isotonic_calibrator.joblib       Калибратор вероятностей
    ├── catboost_regressor.cbm           Модель регрессии RUL
    ├── balanced_random_forest.joblib    Страховочный классификатор
    └── model_config.json                Метрики и оптимальный порог t*
```

## Установка

```bash
pip install -r predictive_maintenance/requirements.txt
```

## Интеграция: минимальный пример

```python
import pandas as pd
from predictive_maintenance import PredictiveMaintenanceModel

# Загрузка обученной модели
model = PredictiveMaintenanceModel.load("predictive_maintenance/models")

# Входной фрейм с 67 признаками (порядок не важен, главное — наличие столбцов)
df = pd.read_parquet("new_data.parquet")

# Совмещённый прогноз
result = model.predict(df)
# Колонки: failure_probability, failure_label, remaining_useful_life_days
```

## Раздельные вызовы

```python
# Прогноз отказа
failure = model.predict_failure(df)               # порог t* по умолчанию
failure = model.predict_failure(df, threshold=0.5) # пользовательский порог
print(failure.probability)   # вероятности
print(failure.label)         # бинарные метки
print(failure.threshold)     # использованный порог

# Прогноз RUL
rul = model.predict_rul(df)
print(rul.rul_days)          # остаточный ресурс, дни
print(rul.rmse_holdout)      # контрольный RMSE на тесте
```

## Требования к входным данным

Фрейм должен содержать 67 признаков из `predictive_maintenance.schema.FEATURE_COLUMNS`.

Признаки `Laser_Intensity`, `Hydraulic_Pressure_bar`, `Coolant_Flow_L_min`,
`Heat_Index` имеют MNAR-структуру: для оборудования без соответствующих
датчиков значение признака не имеет смысла, факт отсутствия должен быть
отражён в индикаторе `*_available = 0`. Восстановление NaN перед инференсом
CatBoost выполняется автоматически.

## Использование примера

```bash
python predictive_maintenance/example.py path/to/data.parquet
```

## Информация о модели

```python
model.info()
# {
#   "version": "1.0.0",
#   "n_features": 67,
#   "classification": {...},
#   "regression": {...}
# }
```

## Версия

1.0.0 — 2026-06-08
