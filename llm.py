"""Тонкая обёртка над Anthropic Messages API с фолбэком при отсутствии ключа."""
from __future__ import annotations

import json
import logging
import threading

from config import CONFIG

logger = logging.getLogger(__name__)

_client = None

# Текст последней ошибки API (None — последний вызов прошёл): для понятного отчёта оператору
last_error: str | None = None
# То же, но для текущего потока: вызовы идут из asyncio.to_thread параллельно (продающий бот,
# ловец), и чужой сбой/успех не должен влиять на решение «ответил ли ИИ на МОЙ вызов»
_local = threading.local()


def thread_call_failed() -> bool:
    """Упал ли последний вызов ИИ в этом потоке (при заданном ключе)."""
    return available() and getattr(_local, "error", None) is not None


def describe_last_error() -> str:
    if not last_error:
        return "ИИ не ответил"
    if "credit balance is too low" in last_error:
        return "на счёте Anthropic закончились деньги — пополните баланс в console.anthropic.com → Billing"
    if "authentication" in last_error.lower() or "api key" in last_error.lower():
        return "ключ Anthropic не подходит — проверьте ANTHROPIC_API_KEY"
    if "overloaded" in last_error.lower() or "rate" in last_error.lower():
        return "Anthropic перегружен или превышен лимит запросов — повторите позже"
    return f"ошибка ИИ: {last_error[:200]}"


def _remember(exc: Exception) -> None:
    """Сбоем ИИ считаем только ошибки API/сети. Кривой JSON в ответе — ИИ работает, просто ответил не так."""
    import anthropic

    if not isinstance(exc, anthropic.APIError):
        _succeeded()
        return
    global last_error
    last_error = str(exc)
    _local.error = last_error


def reset_thread_state() -> None:
    _local.error = None


def _succeeded() -> None:
    global last_error
    last_error = None
    _local.error = None


def available() -> bool:
    return bool(CONFIG.llm_api_key)


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=CONFIG.llm_api_key)
    return _client


def call_json(system_prompt: str, user_message: str, max_tokens: int = 1024, model: str | None = None) -> dict | None:
    """Вызывает LLM, ожидает JSON-ответ. Возвращает None при отсутствии ключа или ошибке."""
    if not available():
        return None
    try:
        client = _get_client()
        resp = client.messages.create(
            model=model or CONFIG.llm_model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        _succeeded()
        return json.loads(text)
    except Exception as exc:
        _remember(exc)
        logger.exception("llm.call_json failed, falling back to heuristics")
        return None


def call_text(system_prompt: str, user_message: str, max_tokens: int = 1024, model: str | None = None) -> str | None:
    if not available():
        return None
    try:
        client = _get_client()
        resp = client.messages.create(
            model=model or CONFIG.llm_model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        _succeeded()
        return "".join(block.text for block in resp.content if block.type == "text").strip()
    except Exception as exc:
        _remember(exc)
        logger.exception("llm.call_text failed, falling back to templates")
        return None
