"""Скоринг П1, генерация касания П5, извлечение заказа, ответ о статусе, ориентир.

Балл скоринга нигде не персистится (см. `materials/Гайд по скорингу лидов.md`: "балл - фильтр
на входящем, а не картотека") - используется только в моменте обработки сообщения.
"""
from __future__ import annotations

import re

from dataclasses import dataclass

import catalog
import kp
import llm
from prompts import (
    BUSINESS_CONTEXT_OG1,
    CLOSING_PROMPT_OG1,
    HEURISTIC_SCORE_KEYWORDS,
    ORIENTIR_TEMPLATE,
    SCORING_SYSTEM_PROMPT,
    STATUS_ANSWER_TEMPLATE,
    MANAGER_STYLE_OG1,
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
    refusal: bool = False  # лид отказался / вопрос уже решён — вежливо закрываем диалог


def band_for_score(score: int) -> str:
    for low, high, band in BAND_BY_RANGE:
        if low <= score <= high:
            return band
    return "cold"


# Номер телефона в свободном тексте: +7 (999) 123-45-67, 89991234567, +995 555 12 34 56 и т.п.
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")


def extract_phone(text: str) -> str | None:
    """Первый похожий на телефон фрагмент (10-15 цифр) или None. Даты и время сюда не попадают:
    в них меньше 10 цифр."""
    for match in PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group())
        if 10 <= len(digits) <= 15:
            return match.group().strip()
    return None


# Тексты в стиле менеджера (prompts.MANAGER_STYLE_OG1): коротко, «Вы» с большой буквы.
# Сначала просим телефон; не хочет звонок — предлагаем тг или почту (решение менеджера 2026-09-28).
CALL_REQUEST_TEXT = (
    "Отлично! Подскажите, пожалуйста, номер телефона и в какое время Вам удобно, "
    "чтобы специалист позвонил и обсудил всё более детально."
)
CALL_REQUEST_AGAIN_TEXT = "Подскажите, пожалуйста, номер телефона и удобное время, чтобы специалист с Вами связался."
PHONE_NUMBER_REQUEST_TEXT = "Хорошо, напишите, пожалуйста, номер телефона, по которому Вам удобно позвонить."
ALTERNATIVES_TEXT = "Хорошо, тогда можно связаться здесь, в тг, или по почте - как Вам удобнее?"
EMAIL_REQUEST_TEXT = "Хорошо, напишите, пожалуйста, адрес почты."
CONTACT_THANKS_TEXT = "Хорошо, передаю Ваши контакты специалисту по данному направлению, он с Вами свяжется."

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_REFUSAL_MARKERS = (
    "не звон", "без звон", "не хочу по телефон", "не по телефон", "не удобно говорить",
    "неудобно говорить", "не могу говорить", "не хочу давать", "не дам номер", "без номер",
    "не люблю звон", "лучше не звон", "лучше напиш", "лучше перепис", "только перепис",
)

# Мессенджеры и почта: способ связи без номера телефона.
_MESSENGER_MARKERS = ("телеграм", "telegram", "тг", "здесь", "сюда", "ватсап", "вотсап", "вацап",
                      "whatsapp", "вайбер", "viber", "max", "почт", "email", "e-mail")

# Ответ на «когда и где удобно связаться» без номера: канал связи или время.
CONTACT_MARKERS = (
    "телеграм", "telegram", "тг", "здесь", "сюда", "в этом чате", "в личк",
    "ватсап", "вотсап", "вацап", "whatsapp", "вайбер", "viber", "max",
    "звон", "номер", "почт", "email", "e-mail",
    "утр", "днём", "днем", "вечер", "обед", "после", "будн", "выходн", "завтра", "сегодня",
    "понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен", "любое время",
)
TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b(?:в|с|до|после)\s+\d{1,2}\b")


def _mentions(text: str, markers) -> bool:
    lowered = f" {text.lower()} "
    return any(re.search(rf"(?<![а-яa-z]){re.escape(m)}", lowered) for m in markers)


def contact_step(text: str, dialog_context: str) -> str | None:
    """Что делать с ответом лида на просьбу о контакте. dialog_context — история ДО этого ответа.
    'done' — контакт есть, передаём специалисту; 'ask_phone' / 'offer_alternatives' / 'ask_email' —
    уточняем; None — это не ответ про контакт (вопрос и т.п.), пусть отвечает бот как обычно."""
    lowered = text.lower()
    refuses_phone = any(m in lowered for m in PHONE_REFUSAL_MARKERS)
    if not refuses_phone and not looks_like_contact(text):
        return None
    if extract_phone(text) or EMAIL_RE.search(text):
        return "done"
    offered = ALTERNATIVES_TEXT in (dialog_context or "")
    if refuses_phone:
        return "done" if offered else "offer_alternatives"
    if _mentions(text, ("почт", "email", "e-mail", "мейл", "имейл")):
        return "done" if EMAIL_REQUEST_TEXT in (dialog_context or "") else "ask_email"
    if _mentions(text, _MESSENGER_MARKERS):
        return "done"
    # Только время / «звоните» без номера: один раз просим номер, потом предлагаем тг или почту.
    if PHONE_NUMBER_REQUEST_TEXT not in (dialog_context or ""):
        return "ask_phone"
    return "done" if offered else "offer_alternatives"


# Согласие на предложение бота связаться со специалистом — сразу к сбору контакта, без оглядки
# на балл: ИИ-оценка «да» бывает и 70, а человек уже согласился.
_YES_MARKERS = ("да", "давайте", "давай", "можно", "хорошо", "конечно", "ок", "окей", "ok", "удобно",
                "согласна", "согласен", "звоните", "позвоните", "пусть позвонит", "было бы здорово",
                "интересно", "не против", "го", "угу", "ага")
_NO_MARKERS = ("нет", "не надо", "не нужно", "не сейчас", "пока не", "позже", "потом", "сам посмотр",
               "сама посмотр", "не звон", "подумаю", "не удобно", "неудобно")


def accepted_specialist_offer(dialog_before: str, text: str) -> bool:
    """Последняя реплика бота предлагала связь со специалистом, а лид коротко согласился."""
    lines = [l for l in (dialog_before or "").splitlines() if l.strip()]
    last = next((l for l in reversed(lines) if l.startswith(("бот:", "менеджер:"))), "")
    offer = last.lower()
    if "?" not in offer or not ("специалист" in offer or "позвон" in offer or "созвон" in offer):
        return False
    answer = f" {text.lower().strip()} "
    if "?" in text or any(m in answer for m in _NO_MARKERS):
        return False
    return any(re.search(rf"(?<![а-яa-z]){re.escape(m)}(?![а-яa-z])", answer) for m in _YES_MARKERS)


def sent_contact_unasked(text: str) -> bool:
    """В сообщении есть телефон или почта — лид сам оставил контакт для связи."""
    return bool(extract_phone(text) or EMAIL_RE.search(text or ""))


def looks_like_contact(text: str) -> bool:
    """Лида попросили о времени и канале связи — похоже ли сообщение на такой ответ.
    Номер телефона — всегда да. Без номера вопрос («а сколько стоит?») — нет: на него ответит бот."""
    if extract_phone(text) or EMAIL_RE.search(text or ""):
        return True
    if "?" in text:
        return False
    lowered = f" {text.lower()} "
    if TIME_RE.search(lowered):
        return True
    return any(re.search(rf"(?<![а-яa-z]){re.escape(m)}", lowered) for m in CONTACT_MARKERS)


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
            refusal=bool(result.get("refusal", False)),
        )
    score = _heuristic_score(text)
    return ScoreResult(score=score, band=band_for_score(score), reasoning="эвристика по ключевым словам", source="heuristic")


def generate_touch(band: str, funnel_stage: str, dialog_text: str) -> str:
    system_prompt = (
        f"{TEMPLATE_A_OG1}\n\n{MANAGER_STYLE_OG1}\n\nЭтап воронки лида: {funnel_stage}. Бэнд готовности: {band}.\n\n"
        f"АКТУАЛЬНЫЕ ТАРИФЫ И ЦЕНЫ og1 (реальные факты, можно называть лиду):\n"
        f"{catalog.tariff_price_summary()}\n\n"
        "Напиши одно короткое сообщение лиду (1-3 предложения) в стиле менеджера выше. "
        "Если лид спрашивает про тарифы или цену - НАЗОВИ конкретные тарифы и цену «от» из списка "
        "выше (не уклоняйся к менеджеру вместо ответа). Не выдумывай цифры, которых нет в списке выше.\n\n"
        "Реплики «менеджер» в диалоге - это живой менеджер og1, который пишет с этого же аккаунта. "
        "Ты продолжаешь разговор от его лица: не здоровайся и не представляйся заново, не повторяй "
        "уже сказанное, отвечай на то, что лид сказал в ответ на слова менеджера."
    )
    generated = llm.call_text(system_prompt, dialog_text)
    if generated:
        return generated
    # Фолбэк без LLM: шаблон догрева по бэнду, нейтральный
    return WARMUP_STEP_TEXTS_OG1.get(1, "Спасибо за интерес! Если появятся вопросы про тарифы og1 - пишите.")


def generate_closing(dialog_text: str) -> str | None:
    """Короткое тёплое завершение диалога после отказа; None, если ИИ не ответил (шаблон не шлём)."""
    return llm.call_text(f"{TEMPLATE_A_OG1}\n\n{MANAGER_STYLE_OG1}\n\n{CLOSING_PROMPT_OG1}", dialog_text)


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
