"""Инкрементальная выгрузка сообщений из открытой Telegram-группы (отдельный аккаунт).

НЕ использует боевую сессию userbot'а продаж (`TG_STRING_SESSION` из main.py) — отдельные
`CATCHER_TG_*` секреты, чтобы риск флуд-лимита/бана не задевал продающий канал
(см. .scratch/lead-catcher-auto/spec.md, user story 6).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChannel, InputPeerChat

import catcher_db
from config import CONFIG

logger = logging.getLogger(__name__)


def _build_client() -> TelegramClient:
    if CONFIG.catcher_tg_string_session:
        return TelegramClient(
            StringSession(CONFIG.catcher_tg_string_session), CONFIG.catcher_tg_api_id, CONFIG.catcher_tg_api_hash
        )
    return TelegramClient("catcher_tg", CONFIG.catcher_tg_api_id, CONFIG.catcher_tg_api_hash)


def message_link(entity, message_id: int, fallback_url: str) -> str:
    """Публичный чат → t.me/<username>/<id>; закрытый супергруппа/канал → t.me/c/<id>/<id>;
    обычная маленькая группа ссылок на сообщения не имеет — остаётся адрес чата."""
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
        return f"https://t.me/c/{entity.id}/{message_id}"
    return fallback_url


# Поиск чата по @username (ResolveUsernameRequest) Telegram лимитирует жёстко: после ~сотни
# запросов в сутки - FloodWait на пару часов. Поэтому найденный peer храним в source_chats,
# а при бане не долбим поиск дальше - иначе блокировка только продлевается.
_resolve_blocked_until: datetime | None = None
# username -> InputPeer из диалогов аккаунта; грузим один раз на процесс, это без ResolveUsername.
_dialog_peers: dict[str, object] | None = None


def _username_from_url(url: str) -> str | None:
    tail = url.strip().rstrip("/").split("t.me/")[-1].lstrip("@")
    if not tail or tail.startswith("+") or "joinchat" in tail or "/" in tail:
        return None
    return tail.lower()


def _cached_peer(source):
    keys = source.keys()
    if "tg_peer_id" not in keys or not source["tg_peer_id"]:
        return None
    if source["tg_peer_type"] == "channel":
        return InputPeerChannel(int(source["tg_peer_id"]), int(source["tg_access_hash"] or 0))
    if source["tg_peer_type"] == "chat":
        return InputPeerChat(int(source["tg_peer_id"]))
    return None


async def _dialog_peer(client: TelegramClient, username: str | None):
    global _dialog_peers
    if username is None:
        return None
    if _dialog_peers is None:
        peers = {}
        try:
            async for dialog in client.iter_dialogs():
                name = getattr(dialog.entity, "username", None)
                if name:
                    peers[name.lower()] = utils.get_input_peer(dialog.entity)
        except Exception:
            logger.warning("не удалось загрузить диалоги ловца, ищу чат по ссылке", exc_info=True)
            return None
        _dialog_peers = peers
    return _dialog_peers.get(username)


async def _resolve_entity(client: TelegramClient, conn, source):
    """Сначала запомненный peer, потом диалоги аккаунта, и только в крайнем случае - поиск по @username."""
    global _resolve_blocked_until
    peer = _cached_peer(source)
    if peer is not None:
        return await client.get_entity(peer)

    peer = await _dialog_peer(client, _username_from_url(source["url"]))
    if peer is not None:
        entity = await client.get_entity(peer)
    else:
        now = datetime.now(timezone.utc)
        if _resolve_blocked_until and now < _resolve_blocked_until:
            minutes = int((_resolve_blocked_until - now).total_seconds() // 60) + 1
            raise RuntimeError(
                f"Telegram временно запретил поиск чатов по ссылке (ещё ~{minutes} мин), "
                "чат подхватится при следующем обходе"
            )
        try:
            entity = await client.get_entity(source["url"])
        except FloodWaitError as e:
            _resolve_blocked_until = now + timedelta(seconds=e.seconds)
            raise RuntimeError(
                f"Telegram временно запретил поиск чатов по ссылке (~{e.seconds // 60 + 1} мин), "
                "чат подхватится при следующем обходе"
            ) from e

    input_peer = utils.get_input_peer(entity)
    if isinstance(input_peer, InputPeerChannel):
        catcher_db.save_tg_peer(conn, source["id"], "channel", input_peer.channel_id, input_peer.access_hash)
    elif isinstance(input_peer, InputPeerChat):
        catcher_db.save_tg_peer(conn, source["id"], "chat", input_peer.chat_id, None)
    return entity


async def fetch_new_messages(conn, source) -> int:
    """Тянет только новые сообщения (offset_id=last_message_id), останавливаясь на уже виденном.
    При FloodWaitError - просто ждёт и продолжает (MVP-решение, без ретраев/ротации сверх этого)."""
    if not CONFIG.catcher_tg_api_id or not CONFIG.catcher_tg_api_hash:
        raise RuntimeError("CATCHER_TG_API_ID/CATCHER_TG_API_HASH не заданы в .env")

    client = _build_client()
    await client.connect()
    try:
        entity = await _resolve_entity(client, conn, source)
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
            messages = []  # после FloodWait итерация начинается заново - без дублей
            try:
                async for message in client.iter_messages(
                    entity, offset_id=offset_id, offset_date=offset_date, reverse=True, limit=None
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
                url=message_link(entity, message.id, source["url"]),
                posted_at=message.date.isoformat() if message.date else None,
                author_username=username,
                author_tg_id=getattr(sender, "id", None),
                reply_to_external_id=str(message.reply_to_msg_id) if message.reply_to_msg_id else None,
            )
            if row_id is not None:
                fetched += 1
                # get_sender ходит в сеть: без промежуточных коммитов выгрузка большого чата
                # держит базу минутами и мешает продающему боту.
                if fetched % 100 == 0:
                    conn.commit()

        if max_seen_id != offset_id:
            catcher_db.update_cursor(conn, source["id"], str(max_seen_id))
        return fetched
    finally:
        await client.disconnect()
