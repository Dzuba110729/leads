"""Предупреждения оператору о сбое ИИ.

Когда Anthropic не отвечает (кончились деньги, неверный ключ, перегрузка), продающий бот
не должен слать живым людям шаблонные ответы вместо нормальных — он молчит, а операторы
получают сообщение в ТГ-агенте (не чаще раза в час). Когда ИИ снова заработал — второе
сообщение со списком тех, кто за время сбоя остался без ответа.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import llm
import runtime
from config import CONFIG

logger = logging.getLogger(__name__)

ALERT_EVERY = timedelta(hours=1)

_outage = False
_last_alert_at: datetime | None = None
_missed: list[str] = []  # кто писал во время сбоя (@username / ID), без повторов


def llm_configured() -> bool:
    """Без ключа вовсе — это не сбой, а осознанный режим эвристик: тогда не молчим."""
    return llm.available()


def run_llm_step(fn, *args):
    """Для asyncio.to_thread: вызвать шаг пайплайна и в том же потоке узнать, ответил ли ИИ.
    Возвращает (результат, ИИ_упал)."""
    llm.reset_thread_state()  # поток из пула мог остаться со сбоем от чужого вызова
    result = fn(*args)
    return result, llm.thread_call_failed()


async def notify_operators(text: str) -> None:
    sent = False
    if CONFIG.agent_bot_token and CONFIG.agent_allowed_ids:
        from aiogram import Bot

        bot = Bot(token=CONFIG.agent_bot_token)
        try:
            for chat_id in CONFIG.agent_allowed_ids:
                try:
                    await bot.send_message(chat_id, text, disable_web_page_preview=True)
                    sent = True
                except Exception:
                    logger.exception("alert: failed to notify %s", chat_id)
        finally:
            await bot.session.close()
    if not sent and CONFIG.operator_chat and runtime.userbot is not None:
        try:
            await runtime.userbot.send_message(CONFIG.operator_chat, text)
            sent = True
        except Exception:
            logger.exception("alert: failed to notify OPERATOR_CHAT")
    if not sent:
        logger.error("alert not delivered: %s", text)


async def report_llm_outage(who: str) -> None:
    """Вызывается, когда продающий бот промолчал из-за сбоя ИИ."""
    global _outage, _last_alert_at
    _outage = True
    if who not in _missed:
        _missed.append(who)
    now = datetime.now(timezone.utc)
    if _last_alert_at is not None and now - _last_alert_at < ALERT_EVERY:
        return
    _last_alert_at = now
    logger.error("LLM outage, sales bot is silent: %s", llm.last_error)
    await notify_operators(
        f"⚠️ ИИ недоступен: {llm.describe_last_error()}.\n\n"
        "Продающий бот сейчас НЕ отвечает людям, чтобы не слать им шаблонные ответы. "
        f"Без ответа пока: {', '.join(_missed)}.\n\n"
        "Когда ИИ заработает, я напишу. Следующее напоминание — не раньше чем через час."
    )


async def report_llm_ok() -> None:
    """Вызывается после успешного ответа ИИ: если до этого был сбой — сообщаем, что всё снова работает."""
    global _outage, _last_alert_at
    if not _outage:
        return
    missed = list(_missed)
    _outage, _last_alert_at = False, None
    _missed.clear()
    text = "✅ ИИ снова работает — продающий бот опять отвечает людям."
    if missed:
        text += (
            f"\n\nВо время сбоя без ответа остались: {', '.join(missed)}. "
            "Бот сам им не напишет — ответьте вручную или дождитесь их следующего сообщения."
        )
    await notify_operators(text)
