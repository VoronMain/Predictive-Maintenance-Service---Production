# -*- coding: utf-8 -*-
"""
charts.py — формирование графических иллюстраций для подсистемы
оповещений.

Модуль строит PNG-изображения динамики ключевых показателей единицы
оборудования (калиброванная вероятность отказа и остаточный ресурс), а
также сводные диаграммы по группе однотипных оповещений. Полученные
изображения встраиваются в HTML-письма (inline, через Content-ID) и
прикладываются к ним отдельным файлом.

Отрисовка выполняется средствами библиотеки matplotlib в неинтерактивном
режиме (backend Agg), что не требует графического окружения и пригодно
для исполнения на стороне сервера.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Iterable, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# Цветовая палитра по степеням критичности (согласована со стилем
# дашбордов подсистемы визуализации).
SEVERITY_COLOR = {
    "critical": "#d1413a",
    "high": "#e0823d",
    "medium": "#e6b800",
    "none": "#3a9a52",
}


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def render_machine_dynamics(history: list[dict], threshold: float,
                            machine_id: str, machine_type: str) -> bytes:
    """Строит график динамики вероятности отказа и RUL по единице
    оборудования и возвращает PNG-изображение в виде набора байтов.

    Parameters
    ----------
    history : list[dict]
        Записи предсказаний (в порядке убывания времени) с полями
        timestamp, failure_probability, remaining_useful_life_days.
    threshold : float
        Порог классификации t*, отображается пунктирной линией.
    """
    rows = sorted(
        (r for r in history if _parse_ts(r.get("timestamp")) is not None),
        key=lambda r: _parse_ts(r["timestamp"]),
    )
    ts = [_parse_ts(r["timestamp"]) for r in rows]
    prob = [float(r["failure_probability"]) for r in rows]
    rul = [float(r["remaining_useful_life_days"]) for r in rows]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(7.2, 4.4), sharex=True, dpi=130,
        gridspec_kw={"hspace": 0.18},
    )
    fig.patch.set_facecolor("white")

    if ts:
        ax1.plot(ts, prob, color="#1f6fb4", marker="o", markersize=3, linewidth=1.6)
        ax1.axhline(threshold, color="#d1413a", linestyle="--", linewidth=1.2,
                    label=f"порог t* = {threshold:.2f}")
        ax1.fill_between(ts, prob, threshold,
                         where=[p >= threshold for p in prob],
                         color="#d1413a", alpha=0.12, interpolate=True)
        ax1.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax1.set_ylabel("Вероятность отказа", fontsize=9)
    ax1.set_ylim(0, 1)
    ax1.grid(True, color="#e6e6e6", linewidth=0.7)
    ax1.set_title(f"{machine_id} ({machine_type}) — динамика показателей",
                  fontsize=10, loc="left")

    if ts:
        ax2.plot(ts, rul, color="#e0823d", marker="o", markersize=3, linewidth=1.6)
    ax2.set_ylabel("RUL, сут.", fontsize=9)
    ax2.grid(True, color="#e6e6e6", linewidth=0.7)
    ax2.set_xlabel("Время", fontsize=9)
    if ts:
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
        for lbl in ax2.get_xticklabels():
            lbl.set_rotation(0)
            lbl.set_fontsize(8)

    for ax in (ax1, ax2):
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    return _to_png(fig)


def render_group_bar(items: Iterable[dict]) -> bytes:
    """Строит горизонтальную столбчатую диаграмму текущей вероятности
    отказа по единицам оборудования группы однотипных оповещений.

    Parameters
    ----------
    items : iterable of dict
        Элементы с полями machine_id, probability, severity.
    """
    rows = sorted(items, key=lambda r: float(r["probability"]))
    ids = [r["machine_id"] for r in rows]
    prob = [float(r["probability"]) for r in rows]
    colors = [SEVERITY_COLOR.get(str(r.get("severity", "medium")), "#e6b800")
              for r in rows]

    height = max(1.6, 0.42 * len(rows) + 0.8)
    fig, ax = plt.subplots(figsize=(7.2, height), dpi=130)
    fig.patch.set_facecolor("white")
    y = range(len(rows))
    ax.barh(list(y), prob, color=colors, height=0.6)
    for i, p in enumerate(prob):
        ax.text(min(p + 0.02, 0.98), i, f"{p:.2f}", va="center", fontsize=8,
                color="#333")
    ax.set_yticks(list(y))
    ax.set_yticklabels(ids, fontsize=8)
    ax.set_xlim(0, 1)
    ax.set_xlabel("Калиброванная вероятность отказа", fontsize=9)
    ax.set_title("Сводка по группе однотипных оповещений", fontsize=10, loc="left")
    ax.grid(True, axis="x", color="#e6e6e6", linewidth=0.7)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    return _to_png(fig)


def _to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
