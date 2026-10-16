"""Напоминания готовому лиду, который согласился на связь со специалистом, но не ответил, как и
когда с ним связаться. Только таким лидам: открытая заявка без контакта, последним писал бот.

Лесенка (от момента, когда лид писал последний раз):
1) через 3,5 часа в тот же день по Москве — ещё раз просим номер и время;
2) на следующий день с 10:00 по Москве — когда удобно созвониться, или специалист напишет сюда;
3) через 3 дня — последнее мягкое сообщение, дальше молчим.
Пишем только с 9 до 22 по Москве. Любой ответ лида сбрасывает лесенку (он снова в разговоре),
«Веду сам», отказ, блокировка или данный контакт — останавливают."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import db

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
WORK_HOURS = (9, 22)
FIRST_AFTER = timedelta(hours=3, minutes=30)
SECOND_FROM_HOUR = 10
LAST_AFTER = timedelta(days=3)
MIN_GAP = timedelta(hours=3)
MAX_AGE = timedelta(days=5)  # старые заявки, о которых забыли, не тревожим
LAST_STAGE = 3

TEXTS = {
    1: "{hi}подскажите, пожалуйста, номер телефона и удобное время - передам специалисту, "
       "он позвонит и всё подробно расскажет.",
    2: "Добрый день{name_tail}! Подскажите, когда Вам удобно созвониться? Если проще в переписке, "
       "специалист может написать прямо сюда, {where}.",
    3: "{hi}не хочу Вас отвлекать. Если вопрос ещё актуален - напишите, когда удобно, и специалист "
       "свяжется. Если уже определились - тоже дайте знать, пожалуйста.",
}


def _greeting_name(lead) -> str | None:
    """Обращение по имени — только если в профиле одно слово, похожее на имя («Илдар»)."""
    name = db.lead_person_name(lead)
    return name if name and len(name.split()) == 1 else None


def reminder_text(stage: int, lead) -> str:
    name = _greeting_name(lead)
    where = "в ВК" if db.is_vk_lead(lead) else "в тг"
    text = TEXTS[stage].format(hi=f"{name}, " if name else "", name_tail=f", {name}" if name else "", where=where)
    return text[0].upper() + text[1:]


def due_stage(anchor: datetime, stage: int, reminded_at: datetime | None, now: datetime) -> int | None:
    """Какое напоминание пора отправить (1-3) или None. anchor — когда лид писал последний раз."""
    now_msk, anchor_msk = now.astimezone(MSK), anchor.astimezone(MSK)
    if not (WORK_HOURS[0] <= now_msk.hour < WORK_HOURS[1]):
        return None
    if now - anchor > MAX_AGE or stage >= LAST_STAGE:
        return None
    if reminded_at is not None and now - reminded_at < MIN_GAP:
        return None
    next_day = now_msk.date() > anchor_msk.date()
    if stage == 0 and not next_day and now - anchor >= FIRST_AFTER:
        return 1
    if stage <= 1 and next_day and now_msk.hour >= SECOND_FROM_HOUR:
        return 2
    if stage == 2 and now - anchor >= LAST_AFTER and now_msk.date() > reminded_at.astimezone(MSK).date():
        return 3
    return None


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def run_once(now: datetime | None = None) -> list[int]:
    """Один проход. Возвращает id заявок, по которым ушло напоминание."""
    import main

    now = now or datetime.now(timezone.utc)
    sent = []
    with db.session() as conn:
        orders = db.orders_for_contact_reminders(conn)
    for order in orders:
        with db.session() as conn:
            lead = db.get_lead(conn, order["lead_id"])
        # Последним писал не бот (лид или менеджер) — напоминать не о чем: бот или менеджер ответят.
        if main.last_turn(lead["dialog_context"] or "")[0] != "бот":
            continue
        anchor = max(_ts(order["created_at"]), _ts(lead["last_seen_at"]) or _ts(order["created_at"]))
        stage, reminded_at = order["contact_reminder_stage"], _ts(order["contact_reminded_at"])
        if reminded_at is not None and anchor > reminded_at:
            stage, reminded_at = 0, None  # лид отвечал после напоминания — лесенка заново
        new_stage = due_stage(anchor, stage, reminded_at, now)
        if new_stage is None:
            if stage != order["contact_reminder_stage"]:
                with db.session() as conn:
                    db.set_contact_reminder(conn, order["id"], stage, None)
            continue
        try:
            await main._reply(main.LeadChannel(lead), reminder_text(new_stage, lead), lead_id=lead["id"])
        except Exception:
            logger.exception("contact reminder %s for order %s failed", new_stage, order["id"])
            continue
        with db.session() as conn:
            db.set_contact_reminder(conn, order["id"], new_stage, now.isoformat())
        logger.info("CONTACT-REMINDER lead=%s order=%s stage=%s", lead["tg_id"], order["id"], new_stage)
        sent.append(order["id"])
    return sent
