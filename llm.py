"""Тонкая обёртка над Anthropic Messages API с фолбэком при отсутствии ключа."""
from __future__ import annotations

import json
import logging

from config import CONFIG

logger = logging.getLogger(__name__)

_client = None


def available() -> bool:
    return bool(CONFIG.llm_api_key)


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=CONFIG.llm_api_key)
    return _client


def call_json(system_prompt: str, user_message: str, max_tokens: int = 300) -> dict | None:
    """Вызывает LLM, ожидает JSON-ответ. Возвращает None при отсутствии ключа или ошибке."""
    if not available():
        return None
    try:
        client = _get_client()
        resp = client.messages.create(
            model=CONFIG.llm_model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception:
        logger.exception("llm.call_json failed, falling back to heuristics")
        return None


def call_text(system_prompt: str, user_message: str, max_tokens: int = 400) -> str | None:
    if not available():
        return None
    try:
        client = _get_client()
        resp = client.messages.create(
            model=CONFIG.llm_model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return "".join(block.text for block in resp.content if block.type == "text").strip()
    except Exception:
        logger.exception("llm.call_text failed, falling back to templates")
        return None
