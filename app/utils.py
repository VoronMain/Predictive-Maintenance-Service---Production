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
    """Добавляет внешний каталог Модели в sys.path, если он фактически существует.

    Пакет predictive_maintenance давно вкомпилирован в дерево репозитория
    (см. каталог predictive_maintenance/ в корне проекта), поэтому
    `import predictive_maintenance` работает и без этой функции, если
    процесс запущен из корня репозитория. Обращение к каталогу Модели на
    два уровня выше app/ — это только резервная совместимость со старой
    раскладкой на машине автора (репозиторий и Модели лежат рядом). Если
    такого каталога нет — например, в Docker-образе или в копии
    репозитория без соседних каталогов — функция ничего не делает и не
    бросает исключений.
    """
    models_dir = Path(__file__).resolve().parents[2] / "Модели"
    if models_dir.is_dir():
        models_dir_str = str(models_dir)
        if models_dir_str not in sys.path:
            sys.path.insert(0, models_dir_str)
