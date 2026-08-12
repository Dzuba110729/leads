"""Защита от prompt-инъекций (ТЗ 4.8/8.2) — вызывается до любого другого LLM-шага."""
from __future__ import annotations

from dataclasses import dataclass

import llm
from prompts import GUARD_HIGH_CONFIDENCE_MARKERS, GUARD_SYSTEM_PROMPT


@dataclass
class GuardResult:
    is_injection: bool
    reasoning: str
    source: str  # 'heuristic' | 'llm' | 'fallback_pass'


def _heuristic_check(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in GUARD_HIGH_CONFIDENCE_MARKERS)


def check(text: str) -> GuardResult:
    if _heuristic_check(text):
        return GuardResult(True, "высокоточный маркер инъекции найден эвристикой", "heuristic")

    result = llm.call_json(GUARD_SYSTEM_PROMPT, text)
    if result is not None:
        return GuardResult(
            is_injection=bool(result.get("is_injection", False)),
            reasoning=str(result.get("reasoning", "")),
            source="llm",
        )

    # Деградированный режим без LLM (раздел 10 ТЗ): без явного маркера — пропускаем.
    return GuardResult(False, "LLM недоступен, эвристика маркеров не сработала", "fallback_pass")
