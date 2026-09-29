"""Запуск: userbot (шаги 2-3) + бот-хэндофф (bot.py) + веб-CRM (crm.py) + фон (reminders, warmup).

Один asyncio-процесс, общая SQLite-база — см. CLAUDE.md "Планируемая архитектура реализации".
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
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
import voice
from config import CONFIG
from crm import app as crm_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


def _is_group_signal(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in CONFIG.group_signal_words)


async def handle_incoming(event, source: str) -> None:
    text = (event.raw_text or "").strip()
    if not text and (source != "dm" or not event.media):
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

    who = f"@{username}" if username else f"ID {tg_id}"
    with db.session() as conn:
        lead = db.get_or_create_lead(conn, tg_id, username, source=source)
        if source == "dm":
            db.set_lead_name(conn, lead["id"], db.display_name(sender))
        if lead["blocked"]:
            return
        if not db.claim_incoming_message(conn, lead["id"], event.id):
            return  # уже обработано — догонялка после перезапуска прошла по нему повторно

    if not text:
        text = await _media_to_text(event, lead, who)
        if text is None:
            return

    with db.session() as conn:
        dialog_context = db.append_dialog_context(conn, lead["id"], "лид", text)
    if lead["manual_mode"]:
        return  # «Веду сам»: реплику сохранили для контекста, отвечает менеджер

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

    # Шаг 4 (og1): бот попросил номер и время для звонка — лид прислал номер.
    if await _take_contact_if_awaited(event, lead, text, who):
        return

    # Лид сам прислал телефон или почту (например, в ответ на «оставьте контакты») — сразу к специалисту.
    if not guard_outage and pipeline.sent_contact_unasked(text):
        given = pipeline.ScoreResult(score=90, band="very_hot", reasoning="сам оставил контакт", source="rule")
        with db.session() as conn:
            db.set_lead_score(conn, lead["id"], given.score)
        logger.info("CONTACT-UNASKED lead=%s: сам прислал контакт", tg_id)
        await _escalate(event, lead, dialog_context, given, who, contact_text=text)
        return

    # Лид согласился на предложение связаться со специалистом — сразу просим контакт.
    if not guard_outage and pipeline.accepted_specialist_offer(lead["dialog_context"] or "", text):
        agreed = pipeline.ScoreResult(score=85, band="very_hot", reasoning="согласился на связь со специалистом",
                                      source="rule")
        with db.session() as conn:
            db.set_lead_score(conn, lead["id"], agreed.score)
        logger.info("AGREED lead=%s: согласился на связь со специалистом", tg_id)
        await _escalate(event, lead, dialog_context, agreed, who)
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
        if score_result.refusal and not lead["declined_at"]:
            await _close_politely(event, lead, dialog_context, who)
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


async def _close_politely(event, lead, dialog_context: str, who: str) -> None:
    """Лид отказался или вопрос уже решён: одно тёплое завершение вместо молчания, прогрев стоп."""
    closing, failed = await asyncio.to_thread(alerts.run_llm_step, pipeline.generate_closing, dialog_context)
    if failed or not closing:
        await alerts.report_llm_outage(who)
        return
    await _reply(event, closing, lead_id=lead["id"])
    with db.session() as conn:
        db.mark_declined(conn, lead["id"])
    logger.info("DECLINED lead=%s: отказ, отправлено вежливое завершение", lead["tg_id"])


# Голосовые длиннее не расшифровываем: это минуты работы процессора ради одного сообщения
VOICE_MAX_SECONDS = 300


def _media_kind(event) -> str:
    if event.voice:
        return "голосовое"
    if event.video_note:
        return "видеосообщение"
    if event.sticker:
        return "стикер"
    if event.gif:
        return "GIF"
    if event.photo:
        return "фото"
    if event.video:
        return "видео"
    if event.document:
        return "файл"
    return "вложение"


async def _transcribe(event) -> str | None:
    duration = getattr(event.file, "duration", None) or 0
    if duration > VOICE_MAX_SECONDS:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = await event.download_media(file=tmp)
        if not path:
            return None
        return await asyncio.to_thread(voice.transcribe, path)


async def _media_to_text(event, lead, who: str) -> str | None:
    """Сообщение без текста. Голосовое и кружок расшифровываем и дальше обрабатываем как текст.
    Стикер, фото, файл — бот на них не отвечает: ставим пометку в истории и зовём менеджера."""
    kind = _media_kind(event)
    if kind in ("голосовое", "видеосообщение"):
        text = await _transcribe(event)
        if text:
            logger.info("VOICE lead=%s: %s расшифровано (%s симв.)", lead["tg_id"], kind, len(text))
            return f"[{kind}] {text}"
        kind += ", не удалось расшифровать"
    with db.session() as conn:
        db.append_dialog_context(conn, lead["id"], "лид", f"[{kind}]")
    logger.info("MEDIA lead=%s: %s, бот не отвечает", lead["tg_id"], kind)
    if not lead["manual_mode"]:
        await alerts.notify_operators(
            f"📎 {who} прислал(а) {kind}. Бот на такое не отвечает — посмотрите сами в переписке "
            "продающего аккаунта и ответьте, если нужно."
        )
    return None


# Автоответ продавца тоже приходит в обработчик исходящих, а его id запоминается только после
# отправки — ждём немного, прежде чем решать, кто написал: бот или менеджер.
AUTO_SENT_SETTLE_SECONDS = 2


async def handle_outgoing(event, catch_up: bool = False) -> None:
    """Сообщение, которое менеджер сам написал лиду с продающего аккаунта (с телефона или компьютера).
    Пишем его в историю диалога, чтобы ИИ знал, с чем менеджер зашёл, и продолжал разговор с этого места.
    catch_up — прогон догонялки: id автоответов прошлого запуска не известны, поэтому то, что уже
    есть в истории (ответ бота или уже записанная реплика менеджера), пропускаем по тексту."""
    text = (event.raw_text or "").strip()
    if not text or str(event.chat_id) == str(CONFIG.operator_chat):
        return
    if not catch_up:
        await asyncio.sleep(AUTO_SENT_SETTLE_SECONDS)
        if runtime.is_auto_sent(event.chat_id, event.id):
            return
    chat = await event.get_chat()
    if getattr(chat, "bot", False) or getattr(chat, "is_self", False):
        return
    with db.session() as conn:
        lead = db.ensure_lead(conn, chat.id, getattr(chat, "username", None))
        db.set_lead_name(conn, lead["id"], db.display_name(chat))
        if catch_up and text in (lead["dialog_context"] or ""):
            return
        db.append_dialog_context(conn, lead["id"], "менеджер", text)
    logger.info("MANAGER lead=%s: сообщение менеджера записано в диалог", chat.id)


async def _take_contact_if_awaited(event, lead, text: str, who: str) -> bool:
    """Лид ответил на «когда и где удобно связаться»: сохраняем в заявку, зовём менеджера, бот
    замолкает. Не похоже на такой ответ — False: сообщение идёт обычным путём (лид мог задать вопрос)."""
    with db.session() as conn:
        order = db.order_awaiting_contact(conn, lead["id"])
    if order is None:
        return False
    # История без только что записанной реплики лида — чтобы понять, о чём бот уже спрашивал.
    history = (lead["dialog_context"] or "")
    step = pipeline.contact_step(text, history)
    if step is None:
        return False
    follow_up = {
        "ask_phone": pipeline.PHONE_NUMBER_REQUEST_TEXT,
        "offer_alternatives": pipeline.ALTERNATIVES_TEXT,
        "ask_email": pipeline.EMAIL_REQUEST_TEXT,
    }.get(step)
    if follow_up:
        await _reply(event, follow_up, lead_id=lead["id"])
        return True
    with db.session() as conn:
        db.set_order_contact(conn, order["id"], text)
        # Дальше разговор ведёт менеджер: бот не отвечает и не прогревает (как «Веду сам»).
        db.set_manual_mode(conn, lead["id"], True)
    logger.info("CONTACT lead=%s order=%s: контакт/время для связи получены", lead["tg_id"], order["id"])
    await _reply(event, pipeline.CONTACT_THANKS_TEXT, lead_id=lead["id"])
    # Карточка с перепиской — в ленту готовых лидов (@BOT_USERNAME). Не дошла (бот не настроен
    # или у него не нажали /start) — хотя бы коротко через ТГ-агента, чтобы лид не потерялся.
    import megabitra
    import ready_bot

    # Сначала в Megabitra — чтобы результат сразу был в карточке. Сбой там карточку не отменяет.
    if megabitra.enabled():
        with db.session() as conn:
            fresh_order, fresh_lead = db.get_order(conn, order["id"]), db.get_lead(conn, lead["id"])
        result = await megabitra.push_lead(fresh_order, fresh_lead)
        with db.session() as conn:
            db.set_megabitra_result(conn, order["id"], result.get("id") if result.get("status") == "ok" else None,
                                    megabitra.describe(result))

    if await ready_bot.send_ready_card(order["id"]) == 0:
        await alerts.notify_operators(
            f"📞 {who} ответил(а), когда и где удобно связаться — заявка №{order['id']} ({order['tariff']}).\n\n"
            f"«{text}»\n\n"
            "Бот этому лиду больше не отвечает — свяжитесь и ведите диалог сами.\n"
            f"Карточка в @{CONFIG.bot_username} не дошла — нажмите там /start."
        )
    return True


async def _escalate(event, lead, dialog_text: str, score_result: pipeline.ScoreResult, who: str = "",
                    contact_text: str | None = None) -> None:
    """contact_text — лид сам сразу прислал контакт (телефон/почту): заявку заводим и сразу передаём
    специалисту, не переспрашивая номер."""
    # Уже просили номер по открытой заявке — не плодим вторую, просто напоминаем.
    with db.session() as conn:
        pending = db.order_awaiting_contact(conn, lead["id"])
    if pending is not None:
        await _reply(event, pipeline.CALL_REQUEST_AGAIN_TEXT, lead_id=lead["id"])
        return

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
    if contact_text is not None:
        await _take_contact_if_awaited(event, lead, contact_text, who or f"ID {lead['tg_id']}")
        return
    # Для og1 сделку закрывает человек (договор, документы — og1/PLAN.md п.4), поэтому вместо
    # ссылки на бота-кассира просим номер и время для звонка менеджера.
    await _reply(event, pipeline.CALL_REQUEST_TEXT, lead_id=lead["id"])
    await alerts.notify_operators(
        f"🔥 Горячий лид {who or lead['tg_id']} (балл {score_result.score}) — заявка №{order['id']}: "
        f"{extracted.summary}\n\nБот спросил, когда и где удобно связаться, — пришлю ответ."
    )


async def _reply(event, text: str, lead_id: int | None = None) -> None:
    if lead_id is not None:
        with db.session() as conn:
            db.append_dialog_context(conn, lead_id, "бот", text)
    if CONFIG.dry_run:
        logger.info("[DRY_RUN] would reply: %s", text)
        return
    runtime.remember_auto_sent(event.chat_id, await event.reply(text))


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
                    await runtime.send_to_lead(userbot, lead["tg_id"], lead["username"], text)
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


USERBOT_ALIVE_KEY = "userbot_alive_at"
# start() может зависнуть навсегда (например, после обрыва сети или сдвига часов) — тогда
# процесс жив, но ничего не делает, и launchd его не перезапускает. Падаем по таймауту.
USERBOT_START_TIMEOUT = 120
HEARTBEAT_SECONDS = 60
CATCH_UP_MAX = timedelta(days=2)


async def userbot_heartbeat() -> None:
    """Раз в минуту отмечаем, что продавец на связи: догонялка после перезапуска начнёт с этого места."""
    while True:
        with db.session() as conn:
            db.set_meta(conn, USERBOT_ALIVE_KEY, datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def catch_up_missed(userbot, alive_at: str | None) -> int:
    """Личные сообщения, пришедшие, пока процесс был выключен (перезапуск, сбой). Telethon со
    StringSession их после старта не присылает, поэтому проходим по свежим диалогам сами.
    Дважды не отвечаем: входящие отсекает last_in_msg_id, исходящие — сверка с историей."""
    if not alive_at:
        return 0  # первый запуск с догонялкой: точки отсчёта ещё нет
    now = datetime.now(timezone.utc)
    since = max(datetime.fromisoformat(alive_at) - timedelta(seconds=2 * HEARTBEAT_SECONDS), now - CATCH_UP_MAX)
    handled = 0
    try:
        async for dialog in userbot.iter_dialogs(limit=100):
            if not dialog.is_user or dialog.date is None or dialog.date < since:
                continue  # закреплённые диалоги идут первыми, поэтому не break, а continue
            entity = dialog.entity
            if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
                continue
            missed = []
            async for message in userbot.iter_messages(entity, limit=30):
                if message.date < since:
                    break
                missed.append(message)
            for message in reversed(missed):
                if getattr(message, "action", None) is not None:
                    continue  # служебные: «вступил в чат», звонки и т.п.
                if message.out:
                    await handle_outgoing(message, catch_up=True)
                else:
                    await handle_incoming(message, source="dm")
                handled += 1
    except Exception:
        logger.exception("catch-up: проход по пропущенным сообщениям упал")
    logger.info("CATCH-UP: просмотрено сообщений с %s: %s", since.isoformat(timespec="seconds"), handled)
    return handled


def build_userbot() -> TelegramClient | None:
    if not (CONFIG.tg_api_id and CONFIG.tg_api_hash):
        return None
    if CONFIG.tg_string_session:
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(CONFIG.tg_string_session), CONFIG.tg_api_id, CONFIG.tg_api_hash)
    return TelegramClient("og1_userbot", CONFIG.tg_api_id, CONFIG.tg_api_hash)


async def backfill_lead_names(userbot) -> None:
    """Имена лидам, заведённым до появления поля name (для карточек готовых лидов)."""
    with db.session() as conn:
        leads = db.leads_without_name(conn)
    filled = 0
    for lead in leads:
        try:
            entity = await userbot.get_entity(lead["tg_id"])
        except Exception:
            continue  # нет в кэше аккаунта — имя подтянется со следующим сообщением
        with db.session() as conn:
            db.set_lead_name(conn, lead["id"], db.display_name(entity))
        filled += 1
    if leads:
        logger.info("имена лидов: заполнено %s из %s", filled, len(leads))


async def main() -> None:
    with db.session():
        pass  # прогреть/создать схему на старте

    cashier_bot = bot_module.build_bot() if CONFIG.cashier_bot_active else None
    userbot = build_userbot()

    tasks = [daily_warmup_task(userbot), reminders.run_forever(cashier_bot, userbot)]

    if userbot is not None:

        @userbot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def _on_dm(event):
            await handle_incoming(event, source="dm")

        @userbot.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private))
        async def _on_outgoing_dm(event):
            await handle_outgoing(event)

        @userbot.on(events.NewMessage(incoming=True, func=lambda e: e.is_group))
        async def _on_group(event):
            await handle_incoming(event, source="group")

        try:
            await asyncio.wait_for(userbot.start(phone=CONFIG.tg_phone or None), USERBOT_START_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("userbot не подключился за %s с — выходим, launchd перезапустит", USERBOT_START_TIMEOUT)
            raise SystemExit(1)
        runtime.userbot = userbot
        with db.session() as conn:
            alive_at = db.get_meta(conn, USERBOT_ALIVE_KEY)
        tasks.append(catch_up_missed(userbot, alive_at))
        tasks.append(backfill_lead_names(userbot))
        tasks.append(userbot_heartbeat())
        tasks.append(userbot.run_until_disconnected())
    else:
        logger.warning("TG_API_ID/TG_API_HASH не заданы — userbot не запущен")

    # @BOT_USERNAME: лента готовых лидов (+ старый бот-кассир, если его включили обратно).
    if CONFIG.bot_token:
        import ready_bot
        from aiogram import Dispatcher

        feed_bot = cashier_bot or bot_module.build_bot()
        runtime.ready_bot = feed_bot
        if cashier_bot is not None:
            from bot import router_dp as feed_dp
            runtime.cashier_bot_enabled = True
        else:
            feed_dp = Dispatcher()
        feed_dp.include_router(ready_bot.router)
        tasks.append(feed_dp.start_polling(feed_bot))
    else:
        logger.warning("BOT_TOKEN не задан — лента готовых лидов не запущена")

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
