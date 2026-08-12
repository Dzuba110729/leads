"""Запуск: userbot (шаги 2-3) + бот-хэндофф (bot.py) + веб-CRM (crm.py) + фон (reminders, warmup).

Один asyncio-процесс, общая SQLite-база — см. CLAUDE.md "Планируемая архитектура реализации".
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn
from telethon import TelegramClient, events

import bot as bot_module
import db
import guard
import pipeline
import reminders
from config import CONFIG
from crm import app as crm_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


def _is_group_signal(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in CONFIG.group_signal_words)


async def handle_incoming(event, source: str) -> None:
    text = (event.raw_text or "").strip()
    if not text:
        return

    sender = await event.get_sender()
    tg_id = sender.id
    username = getattr(sender, "username", None)

    if source == "group":
        if CONFIG.group_reply_mode == "off":
            return
        if not _is_group_signal(text):
            return

    with db.session() as conn:
        lead = db.get_or_create_lead(conn, tg_id, username, source=source)
        if lead["blocked"]:
            return

    # Шаг 8: защита от инъекций - до любого другого LLM-вызова
    guard_result = guard.check(text)
    if guard_result.is_injection:
        with db.session() as conn:
            db.block_lead(conn, lead["id"], guard_result.reasoning)
        logger.warning("BLOCKED lead=%s reason=%s", tg_id, guard_result.reasoning)
        if CONFIG.operator_chat and not CONFIG.dry_run:
            await event.client.send_message(
                CONFIG.operator_chat, f"⚠️ Заблокирован лид {tg_id} (@{username}): {guard_result.reasoning}"
            )
        return

    # Шаг 2.2: вопрос о статусе заказа - отвечаем по факту, без скоринга
    if pipeline.is_status_question(text):
        with db.session() as conn:
            order = db.latest_order_for_lead(conn, lead["id"])
        if order is not None:
            answer = pipeline.status_answer(order["tariff"], order["status"])
        else:
            answer = "Пока не вижу активных заявок по вашему аккаунту. Если уже подавали заявку - уточните у менеджера."
        await _reply(event, answer)
        return

    # Шаг 2.3: скоринг П1
    score_result = pipeline.score_message(text)
    logger.info(
        "SCORE lead=%s score=%s band=%s source=%s reasoning=%s",
        tg_id, score_result.score, score_result.band, score_result.source, score_result.reasoning,
    )

    if score_result.band == "cold":
        return  # молчим

    if score_result.band == "very_hot":
        await _escalate(event, lead, text, score_result)
        return

    # тёплый/горячий - касание П5
    with db.session() as conn:
        lead_row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead["id"],)).fetchone()

    if lead_row["last_touch_at"]:
        from datetime import datetime, timezone
        last_touch = datetime.fromisoformat(lead_row["last_touch_at"])
        gap_hours = (datetime.now(timezone.utc) - last_touch).total_seconds() / 3600
        if gap_hours < CONFIG.min_touch_gap_hours:
            return  # story 17: минимальный зазор между касаниями

    funnel_stage = "интерес"  # PLACEHOLDER: воронка не детализирована пользователем (см. og1/PLAN.md п.6)
    touch = pipeline.generate_touch(score_result.band, funnel_stage, text)
    await _reply(event, touch)
    with db.session() as conn:
        db.record_touch(conn, lead["id"], next_step_idx=0)


async def _escalate(event, lead, dialog_text: str, score_result: pipeline.ScoreResult) -> None:
    extracted = pipeline.extract_order(dialog_text)
    with db.session() as conn:
        order = db.create_order(
            conn,
            lead_id=lead["id"],
            tariff=extracted.tariff_name,
            price=extracted.price,
            department=extracted.department,
            needs_estimator=extracted.needs_estimator,
            summary=extracted.summary,
        )
        db.create_handoff(conn, order["id"], lead["id"], score_result.score, extracted.summary)

    logger.info("ESCALATE lead=%s order=%s tariff=%s", lead["tg_id"], order["id"], extracted.tariff_name)
    link = bot_module.deep_link_for_order(order["id"])
    await _reply(event, f"Похоже, вы готовы двигаться дальше! Продолжим здесь: {link}")


async def _reply(event, text: str) -> None:
    if CONFIG.dry_run:
        logger.info("[DRY_RUN] would reply: %s", text)
        return
    await event.reply(text)


async def daily_warmup_task() -> None:
    if not CONFIG.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=0), warmup loop not started")
        return
    while True:
        with db.session() as conn:
            leads = db.active_leads_for_warmup(conn)
            for lead in leads:
                from datetime import datetime, timezone
                if not lead["last_touch_at"]:
                    continue
                last_touch = datetime.fromisoformat(lead["last_touch_at"])
                silence_days = (datetime.now(timezone.utc) - last_touch).days
                text = pipeline.warmup_step_text(silence_days)
                if text is None:
                    continue
                logger.info("[DRY_RUN warmup] lead=%s silence=%sd text=%s", lead["tg_id"], silence_days, text)
                db.record_touch(conn, lead["id"], next_step_idx=lead["next_step_idx"] + 1)
        await asyncio.sleep(24 * 3600)


async def main() -> None:
    with db.session():
        pass  # прогреть/создать схему на старте

    cashier_bot = bot_module.build_bot() if CONFIG.bot_token else None

    tasks = [daily_warmup_task(), reminders.run_forever(cashier_bot)]

    if CONFIG.tg_api_id and CONFIG.tg_api_hash:
        if CONFIG.tg_string_session:
            from telethon.sessions import StringSession
            userbot = TelegramClient(StringSession(CONFIG.tg_string_session), CONFIG.tg_api_id, CONFIG.tg_api_hash)
        else:
            userbot = TelegramClient("og1_userbot", CONFIG.tg_api_id, CONFIG.tg_api_hash)

        @userbot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def _on_dm(event):
            await handle_incoming(event, source="dm")

        @userbot.on(events.NewMessage(incoming=True, func=lambda e: e.is_group))
        async def _on_group(event):
            await handle_incoming(event, source="group")

        await userbot.start(phone=CONFIG.tg_phone or None)
        tasks.append(userbot.run_until_disconnected())
    else:
        logger.warning("TG_API_ID/TG_API_HASH не заданы — userbot не запущен")

    if cashier_bot is not None:
        from bot import router_dp
        tasks.append(router_dp.start_polling(cashier_bot))
    else:
        logger.warning("BOT_TOKEN не задан — бот-хэндофф не запущен")

    config = uvicorn.Config(crm_app, host=CONFIG.crm_host, port=CONFIG.crm_port, log_level="info")
    server = uvicorn.Server(config)
    tasks.append(server.serve())

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
