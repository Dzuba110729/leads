"""Движок расчёта КП для «Индивидуальных репетиторов» og1 (аналог шага 9 ТЗ).

LLM только матчит параметры из диалога (предметы, часы в неделю, срок в неделях),
код детерминированно считает сумму: Работы -> + накладные% -> Себестоимость -> + наценка% -> КП.
Ставка (catalog.TUTOR_RATE_PLACEHOLDER = 950 ₽/час) подтверждена заказчиком 2026-08-12.
Проценты OVERHEAD_PCT/MARKUP_PCT по-прежнему черновые дефолты, отдельно не подтверждались.
"""
from __future__ import annotations

from dataclasses import dataclass

import llm
from catalog import TUTOR_RATE_PLACEHOLDER

OVERHEAD_PCT = 0.15
MARKUP_PCT = 0.25

EXTRACT_SYSTEM_PROMPT = """\
Извлеки из диалога параметры заказа на индивидуального репетитора og1: сколько предметов,
сколько часов в неделю суммарно по всем предметам, на сколько недель (если не сказано - null).
Не выдумывай числа, которых нет в диалоге - используй null, если не понятно.
Ответь строго JSON: {"subjects": <int|null>, "hours_per_week": <number|null>, "weeks": <int|null>}
"""


@dataclass
class KpEstimate:
    price: float
    hours_per_week: float
    weeks: int
    subjects: int
    needs_estimator: bool


def compute(hours_per_week: float, weeks: float, subjects: int = 1) -> float:
    work_cost = TUTOR_RATE_PLACEHOLDER * hours_per_week * weeks * subjects
    with_overhead = work_cost * (1 + OVERHEAD_PCT)
    return round(with_overhead * (1 + MARKUP_PCT), -1)  # округление до 10 ₽


def estimate_from_dialog(dialog_text: str) -> KpEstimate:
    """Пытается LLM-матчем достать параметры; если не хватает данных — needs_estimator=True."""
    extracted = llm.call_json(EXTRACT_SYSTEM_PROMPT, dialog_text)
    subjects = extracted.get("subjects") if extracted else None
    hours = extracted.get("hours_per_week") if extracted else None
    weeks = extracted.get("weeks") if extracted else None

    if subjects and hours and weeks:
        return KpEstimate(
            price=compute(hours, weeks, subjects),
            hours_per_week=hours,
            weeks=weeks,
            subjects=subjects,
            needs_estimator=False,
        )

    # Недостаточно данных для точного расчёта -> ставим дефолт-ориентир, но помечаем needs_estimator,
    # чтобы технолог/менеджер поставил цену вручную в CRM (см. ТЗ 4.3, needs_estimator).
    default_subjects = subjects or 1
    default_hours = hours or 2.0
    default_weeks = weeks or 4
    return KpEstimate(
        price=compute(default_hours, default_weeks, default_subjects),
        hours_per_week=default_hours,
        weeks=default_weeks,
        subjects=default_subjects,
        needs_estimator=True,
    )
