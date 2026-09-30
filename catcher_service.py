"""Общая логика «выгрузить чат → разобрать через П1» для CRM и ТГ-агента.

Выгрузка Telegram идёт на event loop (Telethon асинхронный), VK и прогон через LLM —
в отдельном потоке со своим sqlite-соединением, чтобы не морозить процесс.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import catcher_db
import catcher_pipeline
import db

logger = logging.getLogger(__name__)


@dataclass
class SourceRunResult:
    source_id: int
    url: str
    fetched: int
    new_candidates: int
    error: str | None = None
    unprocessed: int = 0  # сообщения, до которых ИИ не добрался — разберутся при следующем обходе
    llm_problem: str | None = None


async def run_source(source_id: int) -> SourceRunResult:
    with db.session() as conn:
        catcher_db.migrate(conn)
        source = catcher_db.get_source(conn, source_id)
        if source is None:
            raise ValueError(f"source {source_id} not found")
        url = source["url"]
        try:
            if source["platform"] == "tg":
                import catcher_tg

                fetched = await catcher_tg.fetch_new_messages(conn, source)
            else:
                import catcher_vk

                # sqlite3-соединение нельзя передавать в другой поток — открываем своё внутри
                def _fetch_vk() -> int:
                    with db.session() as thread_conn:
                        return catcher_vk.fetch_new_messages(thread_conn, source)

                fetched = await asyncio.to_thread(_fetch_vk)
        except Exception as exc:
            logger.exception("fetch failed for source %s", source_id)
            return SourceRunResult(source_id, url, 0, 0, error=f"выгрузка: {exc}")

    def _run_pipeline() -> int:
        with db.session() as thread_conn:
            return catcher_pipeline.process_source(thread_conn, source_id)

    try:
        new_candidates = await asyncio.to_thread(_run_pipeline)
    except Exception as exc:
        logger.exception("pipeline failed for source %s", source_id)
        return SourceRunResult(source_id, url, fetched or 0, 0, error=f"разбор: {exc}")
    with db.session() as conn:
        unprocessed = len(catcher_db.unprocessed_messages_for_source(conn, source_id))
    problem = catcher_pipeline.describe_last_error() if unprocessed else None
    return SourceRunResult(source_id, url, fetched or 0, new_candidates, unprocessed=unprocessed, llm_problem=problem)


async def run_sources(source_ids: list[int] | None = None) -> list[SourceRunResult]:
    """None → все включённые источники. Источники идут по очереди: одна Telegram-сессия."""
    if source_ids is None:
        with db.session() as conn:
            catcher_db.migrate(conn)
            source_ids = [s["id"] for s in catcher_db.list_sources(conn) if s["enabled"]]
    results = []
    for source_id in source_ids:
        results.append(await run_source(source_id))
    return results
