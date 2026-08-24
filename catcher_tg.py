"""Инкрементальная выгрузка сообщений из открытой Telegram-группы (отдельный аккаунт).

НЕ использует боевую сессию userbot'а продаж (`TG_STRING_SESSION` из main.py) — отдельные
`CATCHER_TG_*` секреты, чтобы риск флуд-лимита/бана не задевал продающий канал
(см. .scratch/lead-catcher-auto/spec.md, user story 6).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

import catcher_db
from config import CONFIG

logger = logging.getLogger(__name__)


def _build_client() -> TelegramClient:
    if CONFIG.catcher_tg_string_session:
        return TelegramClient(
            StringSession(CONFIG.catcher_tg_string_session), CONFIG.catcher_tg_api_id, CONFIG.catcher_tg_api_hash
        )
    return TelegramClient("catcher_tg", CONFIG.catcher_tg_api_id, CONFIG.catcher_tg_api_hash)


async def fetch_new_messages(conn, source) -> int:
    """Тянет только новые сообщения (offset_id=last_message_id), останавливаясь на уже виденном.
    При FloodWaitError - просто ждёт и продолжает (MVP-решение, без ретраев/ротации сверх этого)."""
    if not CONFIG.catcher_tg_api_id or not CONFIG.catcher_tg_api_hash:
        raise RuntimeError("CATCHER_TG_API_ID/CATCHER_TG_API_HASH не заданы в .env")

    client = _build_client()
    await client.connect()
    try:
        entity = await client.get_entity(source["url"])
        offset_id = int(source["last_message_id"]) if source["last_message_id"] else 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG.catcher_max_message_age_days)
        # При первой выгрузке (offset_id=0) reverse=True без даты читает историю чата
        # с самого начала - может оказаться многолетней давности. offset_date прыгает
        # сразу к точке отсечки, дальше идём вперёд по времени как обычно.
        offset_date = cutoff if offset_id == 0 else None
        fetched = 0
        max_seen_id = offset_id
        messages = []
        while True:
            try:
                async for message in client.iter_messages(
                    entity, offset_id=offset_id, offset_date=offset_date, reverse=True, limit=200
                ):
                    if not message.text:
                        continue
                    if message.date and message.date < cutoff:
                        continue  # защитный фильтр на случай, если offset_date сработал не так
                    messages.append(message)
                    max_seen_id = max(max_seen_id, message.id)
                break
            except FloodWaitError as e:
                logger.warning("FloodWaitError: жду %s сек и продолжаю", e.seconds)
                import asyncio

                await asyncio.sleep(e.seconds)
                continue

        for message in messages:
            sender = await message.get_sender()
            username = getattr(sender, "username", None)
            author = username or getattr(sender, "first_name", None)
            row_id = catcher_db.insert_raw_message(
                conn,
                source_chat_id=source["id"],
                external_id=str(message.id),
                author=author,
                text=message.text,
                url=f"{source['url'].rstrip('/')}/{message.id}",
                posted_at=message.date.isoformat() if message.date else None,
                author_username=username,
                author_tg_id=getattr(sender, "id", None),
            )
            if row_id is not None:
                fetched += 1

        if max_seen_id != offset_id:
            catcher_db.update_cursor(conn, source["id"], str(max_seen_id))
        return fetched
    finally:
        await client.disconnect()
