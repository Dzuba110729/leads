"""Разовый интерактивный логин userbot'а (создаёт локальный .session-файл Telethon)."""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from config import CONFIG


async def main() -> None:
    if not CONFIG.tg_api_id or not CONFIG.tg_api_hash:
        raise SystemExit("Заданы не все TG_API_ID/TG_API_HASH в .env")

    client = TelegramClient("og1_userbot", CONFIG.tg_api_id, CONFIG.tg_api_hash)
    await client.start(phone=CONFIG.tg_phone or None)
    me = await client.get_me()
    print(f"Успешный вход: {me.first_name} (@{me.username}, id={me.id})")
    print("Локальная сессия сохранена в og1_userbot.session — используйте export_session.py, "
          "чтобы получить TG_STRING_SESSION для деплоя без интерактивного логина.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
