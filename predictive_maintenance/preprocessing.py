# -*- coding: utf-8 -*-
"""
preprocessing.py — функции подготовки входных данных для инференса.
"""

import numpy as np

from .schema import MNAR_COLUMNS


def restore_mnar_for_regression(df):
    """Восстанавливает NaN в MNAR-столбцах для модели регрессии CatBoost.

    На этапе предобработки специфические признаки оборудования
    (Laser_Intensity, Hydraulic_Pressure_bar, Coolant_Flow_L_min,
    Heat_Index) были заменены средними значениями, а факт отсутствия
    отражён в столбцах *_available. Модель CatBoost, обученная
    с режимом nan_mode='Min', ожидает NaN-значения в этих столбцах
    там, где соответствующий индикатор равен нулю.

    Parameters
    ----------
    df : pandas.DataFrame
        Входной фрейм после нормализации и one-hot кодировки.

    Returns
    -------
    pandas.DataFrame
        Копия фрейма с восстановленными NaN-значениями.
    """
    out = df.copy()
    for col in MNAR_COLUMNS:
        flag = f"{col}_available"
        if flag in out.columns and col in out.columns:
            mask = out[flag] == 0
            out.loc[mask, col] = np.nan
    return out
