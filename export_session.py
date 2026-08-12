"""Экспорт StringSession из локальной сессии, созданной login.py (для будущего деплоя)."""
from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import CONFIG


async def main() -> None:
    if not CONFIG.tg_api_id or not CONFIG.tg_api_hash:
        raise SystemExit("Заданы не все TG_API_ID/TG_API_HASH в .env")

    client = TelegramClient("og1_userbot", CONFIG.tg_api_id, CONFIG.tg_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована — сначала запустите login.py")
    session_string = StringSession.save(client.session)
    print("TG_STRING_SESSION=" + session_string)
    print("Скопируйте строку выше в .env. Обращайтесь с ней как с паролем — полный доступ к аккаунту.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
