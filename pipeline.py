"""Скоринг П1, генерация касания П5, извлечение заказа, ответ о статусе, ориентир.

Балл скоринга нигде не персистится (см. `materials/Гайд по скорингу лидов.md`: "балл - фильтр
на входящем, а не картотека") - используется только в моменте обработки сообщения.
"""
from __future__ import annotations

from dataclasses import dataclass

import catalog
import kp
import llm
from prompts import (
    BUSINESS_CONTEXT_OG1,
    HEURISTIC_SCORE_KEYWORDS,
    ORIENTIR_TEMPLATE,
    SCORING_SYSTEM_PROMPT,
    STATUS_ANSWER_TEMPLATE,
    TEMPLATE_A_OG1,
    WARMUP_STEP_TEXTS_OG1,
)

BAND_BY_RANGE = [
    (0, 19, "cold"),
    (20, 39, "warm_low"),
    (40, 59, "warm"),
    (60, 79, "hot"),
    (80, 100, "very_hot"),
]

STATUS_MARKERS = ("статус", "мой заказ", "моя заявка", "мою заявку", "где мой", "что с моей заявкой")


@dataclass
class ScoreResult:
    score: int
    band: str
    reasoning: str
    source: str  # 'llm' | 'heuristic'


def band_for_score(score: int) -> str:
    for low, high, band in BAND_BY_RANGE:
        if low <= score <= high:
            return band
    return "cold"


def is_status_question(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in STATUS_MARKERS)


def _heuristic_score(text: str) -> int:
    lowered = text.lower()
    best = 0
    for phrase, score in HEURISTIC_SCORE_KEYWORDS.items():
        if phrase in lowered:
            best = max(best, score)
    return best


def score_message(text: str) -> ScoreResult:
    system_prompt = SCORING_SYSTEM_PROMPT.format(business_context=BUSINESS_CONTEXT_OG1)
    result = llm.call_json(system_prompt, text)
    if result is not None:
        score = max(0, min(100, int(result.get("score", 0))))
        return ScoreResult(
            score=score,
            band=result.get("band") or band_for_score(score),
            reasoning=str(result.get("reasoning", "")),
            source="llm",
        )
    score = _heuristic_score(text)
    return ScoreResult(score=score, band=band_for_score(score), reasoning="эвристика по ключевым словам", source="heuristic")


def generate_touch(band: str, funnel_stage: str, dialog_text: str) -> str:
    system_prompt = (
        f"{TEMPLATE_A_OG1}\n\nЭтап воронки лида: {funnel_stage}. Бэнд готовности: {band}.\n"
        "Напиши одно короткое сообщение-касание лиду (2-4 предложения) голосом бизнеса. "
        "Называй тариф/цену только если они явно фигурируют в диалоге ниже - не выдумывай факты."
    )
    generated = llm.call_text(system_prompt, dialog_text)
    if generated:
        return generated
    # Фолбэк без LLM: шаблон догрева по бэнду, нейтральный
    return WARMUP_STEP_TEXTS_OG1.get(1, "Спасибо за интерес! Если появятся вопросы про тарифы og1 - пишите.")


def warmup_step_text(silence_days: int) -> str | None:
    """Шаг 3 ТЗ: подбирает текст следующего шага прогрева по порогу тишины."""
    if silence_days >= 7:
        return WARMUP_STEP_TEXTS_OG1[7]
    if silence_days >= 3:
        return WARMUP_STEP_TEXTS_OG1[3]
    if silence_days >= 1:
        return WARMUP_STEP_TEXTS_OG1[1]
    return None


@dataclass
class ExtractedOrder:
    tariff_name: str
    price: float | None
    department: str
    needs_estimator: bool
    summary: str


def extract_order(dialog_text: str) -> ExtractedOrder:
    """Шаг 4.1 ТЗ: определяет позицию заказа из диалога. Не хранит сырой текст лида —
    только короткую нейтральную сводку для CRM/оператора."""
    tariff = catalog.find_tariff(dialog_text)
    if tariff is None:
        return ExtractedOrder(
            tariff_name="не определён",
            price=None,
            department="приёмная_комиссия",
            needs_estimator=True,
            summary="Тариф не удалось определить из диалога - требуется уточнение менеджера.",
        )

    department = catalog.route_department(tariff)
    if tariff.name == "Индивидуальные репетиторы":
        estimate = kp.estimate_from_dialog(dialog_text)
        return ExtractedOrder(
            tariff_name=tariff.name,
            price=estimate.price,
            department=department,
            needs_estimator=estimate.needs_estimator,
            summary=(
                f"Репетитор: {estimate.subjects} предм., {estimate.hours_per_week} ч/нед, "
                f"{estimate.weeks} нед."
            ),
        )

    return ExtractedOrder(
        tariff_name=tariff.name,
        price=tariff.price_min,
        department=department,
        needs_estimator=False,
        summary=f"Тариф «{tariff.name}» ({tariff.format}).",
    )


def orientir_price(dialog_text: str) -> str:
    """Read-only ориентир для тёплого лида, спрашивающего цену кастома (репетитор), без заявки."""
    estimate = kp.estimate_from_dialog(dialog_text)
    return ORIENTIR_TEMPLATE.format(price=estimate.price)


def status_answer(tariff: str, status: str) -> str:
    return STATUS_ANSWER_TEMPLATE.format(tariff=tariff, status=status)
