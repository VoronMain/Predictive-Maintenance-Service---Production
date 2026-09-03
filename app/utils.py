# -*- coding: utf-8 -*-
"""utils.py — общие вспомогательные утилиты приложения СПА."""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Optional


def _isnan(value: Optional[float]) -> bool:
    """Возвращает True для None и float('nan')."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _ensure_models_on_path() -> None:
    """Добавляет каталог Модели в sys.path, если он там ещё не присутствует."""
    models_dir = str(Path(__file__).resolve().parents[2] / "Модели")
    if models_dir not in sys.path:
        sys.path.insert(0, models_dir)
