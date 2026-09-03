# -*- coding: utf-8 -*-
"""
schema.py — спецификация входных признаков моделей предиктивного
обслуживания. Содержит упорядоченный список из 67 признаков
и группу признаков MNAR, для которых выполняется специальная
обработка пропусков при инференсе модели регрессии.
"""

# Упорядоченный список признаков (порядок критичен — должен совпадать
# с порядком, использованным при обучении моделей).
FEATURE_COLUMNS = [
    "Operational_Hours",
    "Temperature_C",
    "Vibration_mms",
    "Sound_dB",
    "Oil_Level_pct",
    "Coolant_Level_pct",
    "Power_Consumption_kW",
    "Last_Maintenance_Days_Ago",
    "Maintenance_History_Count",
    "Failure_History_Count",
    "AI_Supervision",
    "Error_Codes_Last_30_Days",
    "Laser_Intensity",
    "Hydraulic_Pressure_bar",
    "Coolant_Flow_L_min",
    "Heat_Index",
    "AI_Override_Events",
    "Laser_Intensity_available",
    "Hydraulic_Pressure_bar_available",
    "Coolant_Flow_L_min_available",
    "Heat_Index_available",
    "mtype_3D_Printer",
    "mtype_AGV",
    "mtype_Automated_Screwdriver",
    "mtype_Boiler",
    "mtype_CMM",
    "mtype_CNC_Lathe",
    "mtype_CNC_Mill",
    "mtype_Carton_Former",
    "mtype_Compressor",
    "mtype_Conveyor_Belt",
    "mtype_Crane",
    "mtype_Dryer",
    "mtype_Forklift_Electric",
    "mtype_Furnace",
    "mtype_Grinder",
    "mtype_Heat_Exchanger",
    "mtype_Hydraulic_Press",
    "mtype_Industrial_Chiller",
    "mtype_Injection_Molder",
    "mtype_Labeler",
    "mtype_Laser_Cutter",
    "mtype_Mixer",
    "mtype_Palletizer",
    "mtype_Pick_and_Place",
    "mtype_Press_Brake",
    "mtype_Pump",
    "mtype_Robot_Arm",
    "mtype_Shrink_Wrapper",
    "mtype_Shuttle_System",
    "mtype_Vacuum_Packer",
    "mtype_Valve_Controller",
    "mtype_Vision_System",
    "mtype_XRay_Inspector",
    "Machine_Age_years",
    "Hours_per_Year",
    "Stress_Index",
    "Fluid_Score",
    "Maintenance_Urgency",
    "Error_Rate",
    "High_Vibration",
    "Low_Oil",
    "High_Temperature",
    "Low_Coolant",
    "Days_Since_Install",
    "Maint_Freq_days",
    "Maintenance_Overdue",
]

# Специфические признаки оборудования с пропусками типа MNAR.
# Значения этих признаков физически не регистрируются на тех типах
# оборудования, где соответствующие датчики не установлены.
MNAR_COLUMNS = [
    "Laser_Intensity",
    "Hydraulic_Pressure_bar",
    "Coolant_Flow_L_min",
    "Heat_Index",
]


def validate_features(df):
    """Проверяет соответствие фрейма ожидаемой схеме признаков.

    Parameters
    ----------
    df : pandas.DataFrame
        Входной фрейм с признаками для инференса.

    Raises
    ------
    ValueError
        Если набор столбцов не соответствует ожидаемой схеме.

    Returns
    -------
    pandas.DataFrame
        Фрейм с признаками в правильном порядке.
    """
    missing = set(FEATURE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(
            f"Отсутствуют признаки: {sorted(missing)}. "
            f"Ожидается {len(FEATURE_COLUMNS)} признаков."
        )
    return df[FEATURE_COLUMNS].copy()
