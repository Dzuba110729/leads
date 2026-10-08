"""Предупреждения оператору о сбое ИИ.

Когда Anthropic не отвечает (кончились деньги, неверный ключ, перегрузка), продающий бот
не должен слать живым людям шаблонные ответы вместо нормальных — он молчит, а операторы
получают сообщение в ТГ-агенте (не чаще раза в час). Когда ИИ снова заработал — второе
сообщение. Тех, кто писал во время сбоя, бот догоняет сам (main.retry_missed_replies): список
хранится в базе, чтобы пережить перезапуск, — иначе человек, который больше не напишет, так и
остаётся без ответа (2026-10-07 так потеряли лида на обрыве интернета).

Предупреждение, которое не удалось доставить (нет сети), тоже не теряется: оно копится в базе и
уходит, как только связь вернётся (resend_undelivered).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import db
import llm
import runtime
from config import CONFIG

logger = logging.getLogger(__name__)

ALERT_EVERY = timedelta(hours=1)

_outage = False
_last_alert_at: datetime | None = None
_missed: list[str] = []  # кто писал во время сбоя (@username / ID), без повторов

# В meta: кому бот должен ответить после сбоя ([{"lead_id", "who"}]) и недоставленные предупреждения.
MISSED_KEY = "llm_missed_leads"
UNDELIVERED_KEY = "undelivered_alerts"
UNDELIVERED_MAX = 20


def llm_configured() -> bool:
    """Без ключа вовсе — это не сбой, а осознанный режим эвристик: тогда не молчим."""
    return llm.available()


def run_llm_step(fn, *args):
    """Для asyncio.to_thread: вызвать шаг пайплайна и в том же потоке узнать, ответил ли ИИ.
    Возвращает (результат, ИИ_упал)."""
    llm.reset_thread_state()  # поток из пула мог остаться со сбоем от чужого вызова
    result = fn(*args)
    return result, llm.thread_call_failed()


async def _send_to_operators(text: str) -> bool:
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
            await send_to_operator_chat(runtime.userbot, text)
            sent = True
        except Exception:
            logger.exception("alert: failed to notify OPERATOR_CHAT")
    return sent


async def send_to_operator_chat(userbot, text: str):
    """OPERATOR_CHAT числом Telethon находит, только если этот чат уже попадался аккаунту в текущем
    запуске (StringSession не хранит кэш). Не нашёл — подгружаем список диалогов и пробуем ещё раз."""
    target = int(CONFIG.operator_chat) if str(CONFIG.operator_chat).lstrip("-").isdigit() else CONFIG.operator_chat
    try:
        return await userbot.send_message(target, text)
    except ValueError:
        await userbot.get_dialogs()
        return await userbot.send_message(target, text)


async def notify_operators(text: str) -> None:
    if await _send_to_operators(text):
        return
    logger.error("alert not delivered, will resend: %s", text)
    with db.session() as conn:
        queued = json.loads(db.get_meta(conn, UNDELIVERED_KEY) or "[]")
        queued.append(text)
        db.set_meta(conn, UNDELIVERED_KEY, json.dumps(queued[-UNDELIVERED_MAX:], ensure_ascii=False))


async def resend_undelivered() -> int:
    """Повторная отправка предупреждений, не дошедших из-за обрыва связи. Возвращает, сколько ушло."""
    with db.session() as conn:
        queued = json.loads(db.get_meta(conn, UNDELIVERED_KEY) or "[]")
    if not queued:
        return 0
    delivered = 0
    for text in queued:
        if not await _send_to_operators(f"(доставлено с опозданием — не было связи)\n\n{text}"):
            break
        delivered += 1
    with db.session() as conn:
        # Пока отправляли, могли добавиться новые — снимаем только доставленные
        current = json.loads(db.get_meta(conn, UNDELIVERED_KEY) or "[]")
        db.set_meta(conn, UNDELIVERED_KEY, json.dumps(current[delivered:], ensure_ascii=False))
    return delivered


def missed_leads() -> list[dict]:
    with db.session() as conn:
        return json.loads(db.get_meta(conn, MISSED_KEY) or "[]")


def remember_missed(lead_id: int, who: str) -> None:
    with db.session() as conn:
        items = json.loads(db.get_meta(conn, MISSED_KEY) or "[]")
        if all(item["lead_id"] != lead_id for item in items):
            items.append({"lead_id": lead_id, "who": who})
            db.set_meta(conn, MISSED_KEY, json.dumps(items, ensure_ascii=False))


def forget_missed(lead_id: int) -> None:
    with db.session() as conn:
        items = json.loads(db.get_meta(conn, MISSED_KEY) or "[]")
        db.set_meta(conn, MISSED_KEY, json.dumps([i for i in items if i["lead_id"] != lead_id], ensure_ascii=False))


async def report_llm_outage(who: str, lead_id: int | None = None) -> None:
    """Вызывается, когда продающий бот промолчал из-за сбоя ИИ. lead_id — ответим ему сами после сбоя."""
    global _outage, _last_alert_at
    _outage = True
    if lead_id is not None:
        remember_missed(lead_id, who)
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
        "Когда ИИ заработает, бот сам ответит им и я напишу. Следующее напоминание — не раньше чем через час."
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
            "Бот ответит им сам в ближайшие минуты — кому ответил, пришлю отдельно."
        )
    await notify_operators(text)
