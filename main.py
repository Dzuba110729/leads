"""Запуск: userbot (шаги 2-3) + бот-хэндофф (bot.py) + веб-CRM (crm.py) + фон (reminders, warmup).

Один asyncio-процесс, общая SQLite-база — см. CLAUDE.md "Планируемая архитектура реализации".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import uvicorn
from telethon import TelegramClient, events

import alerts
import bot as bot_module
import db
import guard
import pipeline
import reminders
import runtime
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
    if getattr(sender, "bot", False):
        return  # игнорируем других ботов (напр. свою же переписку userbot'а с ботом-кассиром)
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
        dialog_context = db.append_dialog_context(conn, lead["id"], "лид", text)

    # Шаг 8: защита от инъекций - до любого другого LLM-вызова.
    # LLM-клиент синхронный, поэтому все его вызовы уходят в поток - иначе на 2-5 с
    # замирает весь процесс (другие лиды, CRM, фоновые циклы).
    guard_result = await asyncio.to_thread(guard.check, text)
    if guard_result.is_injection:
        with db.session() as conn:
            db.block_lead(conn, lead["id"], guard_result.reasoning)
        logger.warning("BLOCKED lead=%s reason=%s", tg_id, guard_result.reasoning)
        if CONFIG.operator_chat and not CONFIG.dry_run:
            await event.client.send_message(
                CONFIG.operator_chat, f"⚠️ Заблокирован лид {tg_id} (@{username}): {guard_result.reasoning}"
            )
        return

    who = f"@{username}" if username else f"ID {tg_id}"
    # Сбой ИИ (кончились деньги, неверный ключ): живым людям шаблонами не отвечаем — молчим
    # и предупреждаем оператора. Ответ о статусе заявки ИИ не нужен, его даём и при сбое.
    guard_outage = guard_result.source == "fallback_pass" and alerts.llm_configured()

    # Шаг 2.2: вопрос о статусе заказа - отвечаем по факту, без скоринга
    if pipeline.is_status_question(text):
        with db.session() as conn:
            order = db.latest_order_for_lead(conn, lead["id"])
        if order is not None:
            answer = pipeline.status_answer(order["tariff"], order["status"])
        else:
            answer = "Пока не вижу активных заявок по вашему аккаунту. Если уже подавали заявку - уточните у менеджера."
        await _reply(event, answer, lead_id=lead["id"])
        return

    if guard_outage:
        await alerts.report_llm_outage(who)
        return

    # Шаг 2.3: скоринг П1 - с учётом контекста диалога, не только последней реплики
    score_result = await asyncio.to_thread(pipeline.score_message, dialog_context)
    if score_result.source != "llm" and alerts.llm_configured():
        await alerts.report_llm_outage(who)
        return
    if score_result.source == "llm":
        await alerts.report_llm_ok()
    logger.info(
        "SCORE lead=%s score=%s band=%s source=%s reasoning=%s",
        tg_id, score_result.score, score_result.band, score_result.source, score_result.reasoning,
    )

    with db.session() as conn:
        db.set_lead_score(conn, lead["id"], score_result.score)

    if score_result.band == "cold":
        return  # молчим

    if score_result.band == "very_hot":
        await _escalate(event, lead, dialog_context, score_result, who)
        return

    # тёплый/горячий - касание П5. min_touch_gap_hours здесь НЕ применяется: это живой ответ
    # на входящее сообщение лида (шаг 2 методики - отвечаем на каждое сообщение по баллу), а не
    # проактивный прогрев затихших (шаг 3, daily_warmup_task) - там зазор по-прежнему действует.
    funnel_stage = "интерес"  # PLACEHOLDER: воронка не детализирована пользователем (см. og1/PLAN.md п.6)
    touch, failed = await asyncio.to_thread(
        alerts.run_llm_step, pipeline.generate_touch, score_result.band, funnel_stage, dialog_context
    )
    if failed:  # generate_touch при сбое тихо подставляет шаблон — такой не шлём
        await alerts.report_llm_outage(who)
        return
    await _reply(event, touch, lead_id=lead["id"])
    with db.session() as conn:
        db.record_touch(conn, lead["id"], next_step_idx=0)


async def _escalate(event, lead, dialog_text: str, score_result: pipeline.ScoreResult, who: str = "") -> None:
    extracted, failed = await asyncio.to_thread(alerts.run_llm_step, pipeline.extract_order, dialog_text)
    if failed:  # без ИИ заявка собралась бы из догадок — ждём, пока ИИ заработает
        await alerts.report_llm_outage(who or f"ID {lead['tg_id']}")
        return
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
    await _reply(event, f"Похоже, вы готовы двигаться дальше! Продолжим здесь: [бот-менеджер]({link})", lead_id=lead["id"])


async def _reply(event, text: str, lead_id: int | None = None) -> None:
    if lead_id is not None:
        with db.session() as conn:
            db.append_dialog_context(conn, lead_id, "бот", text)
    if CONFIG.dry_run:
        logger.info("[DRY_RUN] would reply: %s", text)
        return
    await event.reply(text)


def warmup_silence_days(last_touch_at: str | None) -> int | None:
    if not last_touch_at:
        return None
    last_touch = datetime.fromisoformat(last_touch_at)
    return (datetime.now(timezone.utc) - last_touch).days


async def warmup_once(userbot) -> int:
    """Шаг 3: один проход по затихшим лидам. Возвращает число отправленных касаний."""
    sent = 0
    with db.session() as conn:
        for lead in db.active_leads_for_warmup(conn):
            silence_days = warmup_silence_days(lead["last_touch_at"])
            if silence_days is None:
                continue
            text = pipeline.warmup_step_text(silence_days)
            if text is None:
                continue
            if CONFIG.dry_run or userbot is None:
                logger.info("[DRY_RUN warmup] lead=%s silence=%sd text=%s", lead["tg_id"], silence_days, text)
            else:
                try:
                    await userbot.send_message(lead["tg_id"], text)
                except Exception:
                    logger.exception("warmup send failed lead=%s, skipping", lead["tg_id"])
                    continue
                logger.info("WARMUP lead=%s silence=%sd step=%s", lead["tg_id"], silence_days, lead["next_step_idx"] + 1)
            db.append_dialog_context(conn, lead["id"], "бот", text)
            db.record_touch(conn, lead["id"], next_step_idx=lead["next_step_idx"] + 1)
            conn.commit()
            sent += 1
    return sent


WARMUP_INTERVAL = timedelta(hours=24)


async def daily_warmup_task(userbot, check_every_seconds: int = 300) -> None:
    """Раз в сутки — проход прогрева. Цикл крутится всегда и смотрит на CONFIG.scheduler_enabled
    на каждом шаге, чтобы напоминания можно было включить из ТГ-агента без перезапуска. Время
    последнего прохода — в meta, чтобы перезапуск процесса не вызывал внеочередную рассылку."""
    if not CONFIG.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=0), warmup waits until it is enabled")
    while True:
        if CONFIG.scheduler_enabled:
            try:
                with db.session() as conn:
                    last = db.get_meta(conn, "warmup_last_run")
                due = last is None or datetime.now(timezone.utc) - datetime.fromisoformat(last) >= WARMUP_INTERVAL
                if due:
                    await warmup_once(userbot)
                    with db.session() as conn:
                        db.set_meta(conn, "warmup_last_run", db.now())
            except Exception:
                logger.exception("daily_warmup_task iteration failed")
        await asyncio.sleep(check_every_seconds)


def build_userbot() -> TelegramClient | None:
    if not (CONFIG.tg_api_id and CONFIG.tg_api_hash):
        return None
    if CONFIG.tg_string_session:
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(CONFIG.tg_string_session), CONFIG.tg_api_id, CONFIG.tg_api_hash)
    return TelegramClient("og1_userbot", CONFIG.tg_api_id, CONFIG.tg_api_hash)


async def main() -> None:
    with db.session():
        pass  # прогреть/создать схему на старте

    cashier_bot = bot_module.build_bot() if CONFIG.bot_token else None
    userbot = build_userbot()

    tasks = [daily_warmup_task(userbot), reminders.run_forever(cashier_bot, userbot)]

    if userbot is not None:

        @userbot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def _on_dm(event):
            await handle_incoming(event, source="dm")

        @userbot.on(events.NewMessage(incoming=True, func=lambda e: e.is_group))
        async def _on_group(event):
            await handle_incoming(event, source="group")

        await userbot.start(phone=CONFIG.tg_phone or None)
        runtime.userbot = userbot
        tasks.append(userbot.run_until_disconnected())
    else:
        logger.warning("TG_API_ID/TG_API_HASH не заданы — userbot не запущен")

    if cashier_bot is not None:
        from bot import router_dp
        runtime.cashier_bot_enabled = True
        tasks.append(router_dp.start_polling(cashier_bot))
    else:
        logger.warning("BOT_TOKEN не задан — бот-хэндофф не запущен")

    if CONFIG.agent_bot_token:
        import agent_bot

        if not CONFIG.agent_allowed_ids:
            logger.warning("AGENT_ALLOWED_IDS пуст — агент будет отвечать всем только подсказкой с их ID")
        tasks.append(agent_bot.agent_dp.start_polling(agent_bot.build_agent_bot()))
    else:
        logger.info("AGENT_BOT_TOKEN не задан — ТГ-агент не запущен")

    config = uvicorn.Config(crm_app, host=CONFIG.crm_host, port=CONFIG.crm_port, log_level="info")
    server = uvicorn.Server(config)
    tasks.append(server.serve())

    # SIGTERM гасят только uvicorn и aiogram, а run_until_disconnected ждёт вечно — поэтому
    # при завершении любой части останавливаем остальные, иначе процесс виснет и launchd
    # не может его перезапустить.
    # Фоновые циклы при SCHEDULER_ENABLED=0 завершаются сразу — это нормально; сигнал к остановке —
    # завершение CRM (uvicorn ловит SIGTERM) или падение любой задачи.
    running = [asyncio.ensure_future(t) for t in tasks]
    server_task = running[-1]
    try:
        pending = set(running)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            failed = [t for t in done if not t.cancelled() and t.exception()]
            for t in failed:
                logger.error("задача упала, останавливаем процесс", exc_info=t.exception())
            if failed or server_task in done:
                break
    finally:
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        if userbot is not None:
            await userbot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
