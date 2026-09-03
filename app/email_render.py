# -*- coding: utf-8 -*-
"""email_render.py — формирование текстовых и HTML-тел оповещений.

Ранее тела писем формировались шаблонами Jinja2. В целях отказа от
внешней зависимости jinja2 рендеринг перенесён в чистый Python:
содержимое собирается строковым форматированием, а пользовательские
значения экранируются для HTML-версии средствами стандартного модуля
``html``. Сохранены прежняя структура и оформление сообщений (multipart
текст + HTML со встроенным графиком через Content-ID).

Контракт функций совпадает с прежними контекстами шаблонов:

  * ``render_alert_text`` / ``render_alert_html`` — одиночное оповещение;
  * ``render_group_text`` / ``render_group_html`` — сводное оповещение по
    группе однотипных предаварийных состояний.
"""
from __future__ import annotations

from html import escape


def _e(value) -> str:
    """Экранирует значение для безопасной вставки в HTML."""
    return escape(str(value), quote=True)


# --------------------------------------------------------------------- #
# Одиночное оповещение
# --------------------------------------------------------------------- #
def render_alert_text(ctx: dict) -> str:
    actions = "".join(
        f"  {i}. {action}\n"
        for i, action in enumerate(ctx["actions"], start=1)
    )
    return (
        "СИСТЕМА ПРЕДИКТИВНОЙ АНАЛИТИКИ (СПА) — ОПОВЕЩЕНИЕ\n"
        "==================================================\n\n"
        "Зарегистрировано предаварийное состояние единицы оборудования.\n"
        f"Степень критичности: {ctx['severity_label']}.\n\n"
        f"Идентификатор оборудования : {ctx['machine_id']}\n"
        f"Тип оборудования           : {ctx['machine_type']}\n"
        f"Время предсказания         : {ctx['timestamp']}\n"
        f"Вероятность отказа         : {ctx['probability']:.3f}\n"
        f"Остаточный ресурс (RUL)    : {ctx['rul']:.1f} сут.\n"
        f"Порог классификации t*     : {ctx['threshold']:.2f}\n"
        f"Идентификатор инцидента    : №{ctx['incident_id']}\n\n"
        "Рекомендуемые действия:\n"
        f"{actions}\n"
        "Сообщение сформировано автоматически. Ответ на письмо не требуется.\n"
        "График динамики показателей приложен к письму отдельным файлом.\n"
    )


def render_alert_html(ctx: dict) -> str:
    actions = "".join(
        f'<li style="margin-bottom:4px;">{_e(a)}</li>' for a in ctx["actions"]
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Оповещение СПА</title>
</head>
<body style="margin:0; padding:0; background:#f1f3f6;
             font-family:'Manrope','Segoe UI',Arial,sans-serif; color:#1f2430;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f1f3f6; padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="background:#ffffff; border-radius:8px; overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:{_e(ctx['severity_color'])}; padding:18px 26px; color:#fff;">
            <div style="font-size:13px; letter-spacing:0.5px; opacity:0.9;">
              СИСТЕМА ПРЕДИКТИВНОЙ АНАЛИТИКИ · ОПОВЕЩЕНИЕ
            </div>
            <div style="font-size:21px; font-weight:600; margin-top:4px;">
              Предаварийное состояние · критичность: {_e(ctx['severity_label'])}
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding:22px 26px 6px 26px;">
            <p style="margin:0 0 14px 0; font-size:14px; line-height:1.5;">
              Зарегистрировано предаварийное состояние единицы оборудования
              <strong>{_e(ctx['machine_id'])}</strong> ({_e(ctx['machine_type'])}).
              Степень критичности оповещения определена как
              <strong>{_e(ctx['severity_label'])}</strong>.
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse; font-size:13px; margin-bottom:14px;">
              <tr>
                <td style="padding:7px 10px; background:#f7f8fa; width:46%;
                           border:1px solid #eceef1;">Идентификатор оборудования</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">
                  <strong>{_e(ctx['machine_id'])}</strong></td>
              </tr>
              <tr>
                <td style="padding:7px 10px; background:#f7f8fa;
                           border:1px solid #eceef1;">Тип оборудования</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">{_e(ctx['machine_type'])}</td>
              </tr>
              <tr>
                <td style="padding:7px 10px; background:#f7f8fa;
                           border:1px solid #eceef1;">Время предсказания</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">{_e(ctx['timestamp'])}</td>
              </tr>
              <tr>
                <td style="padding:7px 10px; background:#f7f8fa;
                           border:1px solid #eceef1;">Вероятность отказа</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">
                  <strong>{ctx['probability']:.3f}</strong>
                  (порог t* = {ctx['threshold']:.2f})</td>
              </tr>
              <tr>
                <td style="padding:7px 10px; background:#f7f8fa;
                           border:1px solid #eceef1;">Остаточный ресурс (RUL)</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">
                  {ctx['rul']:.1f} сут.</td>
              </tr>
              <tr>
                <td style="padding:7px 10px; background:#f7f8fa;
                           border:1px solid #eceef1;">Идентификатор инцидента</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">№{_e(ctx['incident_id'])}</td>
              </tr>
            </table>

            <div style="text-align:center; margin:6px 0 16px 0;">
              <img src="cid:{_e(ctx['chart_cid'])}" alt="Динамика показателей"
                   style="max-width:100%; border:1px solid #eceef1; border-radius:4px;">
            </div>

            <div style="font-size:13px; line-height:1.55; margin-bottom:6px;">
              <strong>Рекомендуемые действия:</strong>
              <ol style="margin:8px 0 0 0; padding-left:20px;">
                {actions}
              </ol>
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding:14px 26px 22px 26px; color:#8a93a2; font-size:11px;
                     line-height:1.5; border-top:1px solid #eef0f3;">
            Сообщение сформировано автоматически подсистемой оповещений СПА.
            Ответ на письмо не требуется. Полная динамика показателей приложена
            к письму отдельным файлом.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# --------------------------------------------------------------------- #
# Сводное (групповое) оповещение
# --------------------------------------------------------------------- #
def render_group_text(ctx: dict) -> str:
    machines = "".join(
        f"  - {m['machine_id']}: p={m['probability']:.3f}, "
        f"RUL={m['rul']:.1f} сут."
        + (f", инцидент №{m['incident_id']}" if m.get("incident_id") else "")
        + "\n"
        for m in ctx["machines"]
    )
    actions = "".join(
        f"  {i}. {action}\n"
        for i, action in enumerate(ctx["actions"], start=1)
    )
    return (
        "СИСТЕМА ПРЕДИКТИВНОЙ АНАЛИТИКИ (СПА) — СВОДНОЕ ОПОВЕЩЕНИЕ\n"
        "=========================================================\n\n"
        f"В пределах окна групповой агрегации зарегистрировано {ctx['count']} однотипных\n"
        f"предаварийных состояний оборудования типа «{ctx['machine_type']}».\n"
        f"Степень критичности: {ctx['severity_label']}.\n"
        f"Ключ группировки: {ctx['group_key']}.\n\n"
        "Охваченные единицы оборудования:\n"
        f"{machines}\n"
        "Рекомендуемые действия:\n"
        f"{actions}\n"
        "Сообщение сформировано автоматически. Перечень охваченных единиц оборудования\n"
        "приложен к письму отдельным файлом.\n"
    )


def render_group_html(ctx: dict) -> str:
    rows = "".join(
        f"""<tr>
                <td style="padding:7px 10px; border:1px solid #eceef1;">
                  <strong>{_e(m['machine_id'])}</strong></td>
                <td style="padding:7px 10px; border:1px solid #eceef1; text-align:right;">
                  {m['probability']:.3f}</td>
                <td style="padding:7px 10px; border:1px solid #eceef1; text-align:right;">
                  {m['rul']:.1f}</td>
                <td style="padding:7px 10px; border:1px solid #eceef1;">
                  {('№' + _e(m['incident_id'])) if m.get('incident_id') else '—'}</td>
              </tr>"""
        for m in ctx["machines"]
    )
    actions = "".join(
        f'<li style="margin-bottom:4px;">{_e(a)}</li>' for a in ctx["actions"]
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Сводное оповещение СПА</title>
</head>
<body style="margin:0; padding:0; background:#f1f3f6;
             font-family:'Manrope','Segoe UI',Arial,sans-serif; color:#1f2430;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f1f3f6; padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="660" cellpadding="0" cellspacing="0"
             style="background:#ffffff; border-radius:8px; overflow:hidden;
                    box-shadow:0 1px 4px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:{_e(ctx['severity_color'])}; padding:18px 26px; color:#fff;">
            <div style="font-size:13px; letter-spacing:0.5px; opacity:0.9;">
              СИСТЕМА ПРЕДИКТИВНОЙ АНАЛИТИКИ · СВОДНОЕ ОПОВЕЩЕНИЕ
            </div>
            <div style="font-size:21px; font-weight:600; margin-top:4px;">
              {_e(ctx['count'])} ед. оборудования «{_e(ctx['machine_type'])}» · критичность: {_e(ctx['severity_label'])}
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding:22px 26px 6px 26px;">
            <p style="margin:0 0 14px 0; font-size:14px; line-height:1.5;">
              В пределах окна групповой агрегации зарегистрировано
              <strong>{_e(ctx['count'])}</strong> однотипных предаварийных состояний
              оборудования типа <strong>{_e(ctx['machine_type'])}</strong> со степенью
              критичности <strong>{_e(ctx['severity_label'])}</strong>. Оповещения
              объединены в одно сводное сообщение (ключ группировки:
              <code>{_e(ctx['group_key'])}</code>).
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse; font-size:13px; margin-bottom:14px;">
              <tr style="background:#eef1f5;">
                <th style="padding:8px 10px; text-align:left; border:1px solid #e2e6ea;">Оборудование</th>
                <th style="padding:8px 10px; text-align:right; border:1px solid #e2e6ea;">Вероятность</th>
                <th style="padding:8px 10px; text-align:right; border:1px solid #e2e6ea;">RUL, сут.</th>
                <th style="padding:8px 10px; text-align:left; border:1px solid #e2e6ea;">Инцидент</th>
              </tr>
              {rows}
            </table>

            <div style="text-align:center; margin:6px 0 16px 0;">
              <img src="cid:{_e(ctx['chart_cid'])}" alt="Сводка по группе"
                   style="max-width:100%; border:1px solid #eceef1; border-radius:4px;">
            </div>

            <div style="font-size:13px; line-height:1.55; margin-bottom:6px;">
              <strong>Рекомендуемые действия:</strong>
              <ol style="margin:8px 0 0 0; padding-left:20px;">
                {actions}
              </ol>
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding:14px 26px 22px 26px; color:#8a93a2; font-size:11px;
                     line-height:1.5; border-top:1px solid #eef0f3;">
            Сообщение сформировано автоматически подсистемой оповещений СПА в
            режиме групповой агрегации однотипных событий. Перечень охваченных
            единиц оборудования приложен к письму отдельным файлом.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
