"""Лесенка напоминаний о незавершённой заявке (шаг 6 ТЗ, адаптация под og1).

Триггер для og1 — не «не оплачено», а «заявка создана, но не передана менеджеру»
(см. og1/PLAN.md п.4: цикл сделки не завершается кнопкой, доводим до хэндоффа).
Если бот-кассир недоступен, напоминание шлёт userbot живым текстом (ТЗ 4.5).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import bot as bot_module
import db
from config import CONFIG

logger = logging.getLogger(__name__)

REMINDER_TEXTS = {
    1: "Не забыли про заявку в og1? Если остались вопросы по тарифу — просто напишите, поможем определиться.",
    2: "Напоминаем про вашу заявку в og1 — если ещё актуально, готовы соединить с приёмной комиссией в удобное время.",
    3: "Последнее напоминание: заявка в og1 будет закрыта в течение получаса, если не подтвердить интерес. Если ещё нужна школа для ребёнка — просто ответьте на это сообщение.",
}


def compute_next_action(elapsed_minutes: float, current_stage: int) -> tuple[str | None, int]:
    """Чистая логика лесенки: возвращает (action, new_stage).
    action: None | 'remind' | 'autoclose'."""
    if elapsed_minutes >= CONFIG.reminder_autoclose_minutes:
        return "autoclose", current_stage
    thresholds = CONFIG.reminder_ladder_minutes
    for idx, threshold in enumerate(thresholds):
        stage_num = idx + 1
        if elapsed_minutes >= threshold and current_stage < stage_num:
            return "remind", stage_num
    return None, current_stage


def _elapsed_minutes(created_at: str) -> float:
    created = datetime.fromisoformat(created_at)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


async def run_once(bot: "bot_module.Bot | None") -> None:
    with db.session() as conn:
        orders = db.list_orders_for_reminders(conn)
        for order in orders:
            elapsed = _elapsed_minutes(order["created_at"])
            action, new_stage = compute_next_action(elapsed, order["reminder_stage"])
            if action is None:
                continue
            lead = conn.execute("SELECT * FROM leads WHERE id = ?", (order["lead_id"],)).fetchone()
            if action == "autoclose":
                db.close_order(conn, order["id"])
                logger.info("order %s autoclosed after silence", order["id"])
                continue
            db.bump_order_reminder(conn, order["id"], new_stage)
            text = REMINDER_TEXTS.get(new_stage, REMINDER_TEXTS[3])
            if bot is not None:
                await bot_module.notify_status_change(bot, lead["tg_id"], text)
            else:
                logger.info("[userbot fallback] remind lead %s: %s", lead["tg_id"], text)


async def run_forever(bot: "bot_module.Bot | None", interval_seconds: int = 300) -> None:
    if not CONFIG.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=0), reminders loop not started")
        return
    while True:
        try:
            await run_once(bot)
        except Exception:
            logger.exception("reminders.run_once failed")
        await asyncio.sleep(interval_seconds)
