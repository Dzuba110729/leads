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
import runtime
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


async def _send_reminder(bot, userbot, tg_id: int, text: str) -> None:
    if CONFIG.dry_run:
        logger.info("[DRY_RUN] remind %s: %s", tg_id, text)
        return
    if bot is not None:
        try:
            await bot.send_message(tg_id, text)
            return
        except Exception as exc:
            # Лид, который ни разу не нажал Start у бота-кассира, — штатный случай, а не сбой.
            if userbot is None:
                raise
            logger.info("cashier bot cannot reach %s (%s), falling back to userbot", tg_id, exc)
    if userbot is None:
        raise RuntimeError("no channel to send reminder")
    runtime.remember_auto_sent(tg_id, await userbot.send_message(tg_id, text))


async def run_once(bot: "bot_module.Bot | None", userbot=None) -> None:
    with db.session() as conn:
        orders = db.list_orders_for_reminders(conn)
        for order in orders:
            elapsed = _elapsed_minutes(order["created_at"])
            action, new_stage = compute_next_action(elapsed, order["reminder_stage"])
            if action is None:
                continue
            if action == "autoclose":
                db.close_order(conn, order["id"])
                conn.commit()
                logger.info("order %s autoclosed after silence", order["id"])
                continue
            lead = conn.execute("SELECT * FROM leads WHERE id = ?", (order["lead_id"],)).fetchone()
            text = REMINDER_TEXTS.get(new_stage, REMINDER_TEXTS[3])
            try:
                await _send_reminder(bot, userbot, lead["tg_id"], text)
            except Exception:
                logger.exception("reminder for order %s (lead %s) failed, skipping", order["id"], lead["tg_id"])
                continue
            db.bump_order_reminder(conn, order["id"], new_stage)
            conn.commit()


async def run_forever(bot: "bot_module.Bot | None", userbot=None, interval_seconds: int = 300) -> None:
    # Цикл крутится всегда и проверяет флаг на каждом шаге — его можно включить из ТГ-агента
    if not CONFIG.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=0), reminders wait until it is enabled")
    while True:
        if CONFIG.scheduler_enabled:
            try:
                await run_once(bot, userbot)
            except Exception:
                logger.exception("reminders.run_once failed")
        await asyncio.sleep(interval_seconds)
