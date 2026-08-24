"""Инкрементальная выгрузка постов открытого VK-сообщества (отдельный аккаунт/токен).

Постраничный `wall.get`, останавливается на первом уже виденном `external_id` (VK не даёт
offset_id-курсор как Telegram — сравниваем id постов напрямую). При рейт-лимите (code 6,
"Too many requests per second") — ждём и продолжаем, как договорились для MVP (без ротации
токенов/прокси).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import vk_api
from vk_api.exceptions import ApiError

import catcher_db
from config import CONFIG

logger = logging.getLogger(__name__)

RATE_LIMIT_CODE = 6
RATE_LIMIT_WAIT_SECONDS = 1


def _group_short_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")


def _wall_get_with_retry(vk, **kwargs):
    while True:
        try:
            return vk.wall.get(**kwargs)
        except ApiError as e:
            if e.code == RATE_LIMIT_CODE:
                logger.warning("VK rate limit, жду %s сек и продолжаю", RATE_LIMIT_WAIT_SECONDS)
                time.sleep(RATE_LIMIT_WAIT_SECONDS)
                continue
            raise


def fetch_new_messages(conn, source) -> int:
    if not CONFIG.vk_access_token:
        raise RuntimeError("VK_ACCESS_TOKEN не задан в .env")

    session = vk_api.VkApi(token=CONFIG.vk_access_token)
    vk = session.get_api()
    domain = _group_short_name(source["url"])
    last_seen_id = int(source["last_message_id"]) if source["last_message_id"] else 0
    # VK отдаёт посты от новых к старым - при первой выгрузке (last_seen_id=0) без отсечки
    # по дате пагинация ушла бы в историю сообщества с момента создания
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=CONFIG.catcher_max_message_age_days)).timestamp())

    fetched = 0
    max_seen_id = last_seen_id
    offset = 0
    page_size = 100
    stop = False
    while not stop:
        response = _wall_get_with_retry(vk, domain=domain, count=page_size, offset=offset)
        items = response.get("items", [])
        if not items:
            break
        for post in items:
            if post["id"] <= last_seen_id:
                stop = True
                break
            if post.get("date", 0) < cutoff_ts:
                stop = True
                break
            text = post.get("text", "")
            if not text:
                continue
            row_id = catcher_db.insert_raw_message(
                conn,
                source_chat_id=source["id"],
                external_id=str(post["id"]),
                author=None,
                text=text,
                url=f"https://vk.com/wall-{post.get('owner_id', '')}_{post['id']}",
                posted_at=str(post.get("date")),
            )
            if row_id is not None:
                fetched += 1
            max_seen_id = max(max_seen_id, post["id"])
        offset += page_size

    if max_seen_id != last_seen_id:
        catcher_db.update_cursor(conn, source["id"], str(max_seen_id))
    return fetched
