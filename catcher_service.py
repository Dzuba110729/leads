"""Общая логика «выгрузить чат → разобрать через П1» для CRM и ТГ-агента.

Обход разделён по платформам: у Telegram своя сессия ловца, у VK свой браузерный профиль,
делить им нечего — поэтому ТГ и ВК обходятся независимо и могут идти одновременно.
Внутри платформы сначала выгружаются все чаты (у ТГ — одним подключением), потом идёт разбор ИИ:
выгрузка занимает минуты, а разбор — основное время, и пусть он не держит сессию открытой.
Разбор у обеих платформ общий и идёт по одному источнику за раз — чтобы два обхода
не жгли лимит подписки Claude параллельно.

Выгрузка Telegram идёт на event loop (Telethon асинхронный), VK и прогон через LLM —
в отдельном потоке со своим sqlite-соединением, чтобы не морозить процесс.
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import catcher_db
import catcher_pipeline
import db
from config import CONFIG

logger = logging.getLogger(__name__)

PLATFORMS = ("tg", "vk")
PLATFORM_NAMES = {"tg": "Telegram", "vk": "ВКонтакте"}


@dataclass
class SourceRunResult:
    source_id: int
    url: str
    fetched: int
    new_candidates: int
    error: str | None = None
    unprocessed: int = 0  # сообщения, до которых ИИ не добрался — разберутся при следующем обходе
    llm_problem: str | None = None
    skipped: str | None = None  # источник сознательно не обходили (VK чаще раза в сутки)
    platform: str = "tg"


# Замки создаются лениво: asyncio.Lock нельзя заводить до запуска event loop.
_platform_locks: dict[str, asyncio.Lock] = {}
_llm_lock: asyncio.Lock | None = None


def _platform_lock(platform: str) -> asyncio.Lock:
    if platform not in _platform_locks:
        _platform_locks[platform] = asyncio.Lock()
    return _platform_locks[platform]


def _analysis_lock() -> asyncio.Lock:
    global _llm_lock
    if _llm_lock is None:
        _llm_lock = asyncio.Lock()
    return _llm_lock


def is_running(platform: str) -> bool:
    lock = _platform_locks.get(platform)
    return bool(lock and lock.locked())


def vk_crawl_wait(last_checked_at: str | None, now: datetime | None = None) -> timedelta | None:
    """Сколько ещё ждать до следующего обхода VK-источника; None — можно обходить."""
    if not last_checked_at or CONFIG.vk_crawl_min_hours <= 0:
        return None
    try:
        last = datetime.fromisoformat(last_checked_at)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    left = last + timedelta(hours=CONFIG.vk_crawl_min_hours) - (now or datetime.now(timezone.utc))
    return left if left > timedelta(0) else None


def _vk_skip_reason(source) -> str | None:
    wait = vk_crawl_wait(source["last_checked_at"])
    if wait is None:
        return None
    hours = int(wait.total_seconds() // 3600)
    minutes = int(wait.total_seconds() % 3600 // 60)
    return f"VK обходим не чаще раза в {CONFIG.vk_crawl_min_hours} ч, следующий через {hours} ч {minutes} мин"


def _vk_sales_halt() -> str | None:
    """Диалоги VK остановлены (капча/проверка/выход) — обход того же аккаунта только добавит запросов."""
    import vk_messenger

    if vk_messenger.STATUS.halted:
        return f"диалоги VK остановлены ({vk_messenger.STATUS.halted}) — сначала пройдите проверку и «Возобновить VK»"
    return None


def _vk_access_lost(exc: Exception) -> bool:
    """VK не пускает аккаунт (выход, проверка, капча) — следующие источники упадут так же."""
    import vk_browser

    text = str(exc).lower()
    return vk_browser.NOT_LOGGED_IN.lower() in text or "challenge" in text or "капч" in text


@asynccontextmanager
async def _tg_client(platform: str):
    """Одно подключение Telegram на весь обход вместо connect/disconnect на каждый чат."""
    if platform != "tg":
        yield None
        return
    import catcher_tg

    async with catcher_tg.open_client() as client:
        yield client


async def _fetch(source, tg_client=None) -> int:
    if source["platform"] == "tg":
        import catcher_tg

        with db.session() as conn:
            return await catcher_tg.fetch_new_messages(conn, source, client=tg_client)

    import catcher_vk

    # sqlite3-соединение нельзя передавать в другой поток — открываем своё внутри
    def _fetch_vk() -> int:
        with db.session() as thread_conn:
            fetched = catcher_vk.fetch_new_messages(thread_conn, source)
            catcher_db.mark_checked(thread_conn, source["id"])
            return fetched

    return await asyncio.to_thread(_fetch_vk)


async def _analyze(result: SourceRunResult) -> SourceRunResult:
    def _run_pipeline() -> int:
        with db.session() as thread_conn:
            return catcher_pipeline.process_source(thread_conn, result.source_id)

    async with _analysis_lock():
        try:
            result.new_candidates = await asyncio.to_thread(_run_pipeline)
        except Exception as exc:
            logger.exception("pipeline failed for source %s", result.source_id)
            result.error = f"разбор: {exc}"
            return result
    with db.session() as conn:
        result.unprocessed = len(catcher_db.unprocessed_messages_for_source(conn, result.source_id))
    result.llm_problem = catcher_pipeline.describe_last_error() if result.unprocessed else None
    return result


def _load_sources(platform: str, source_ids: list[int] | None) -> list:
    with db.session() as conn:
        catcher_db.migrate(conn)
        if source_ids is None:
            return [s for s in catcher_db.list_sources(conn) if s["enabled"] and s["platform"] == platform]
        found = [catcher_db.get_source(conn, sid) for sid in source_ids]
    return [s for s in found if s is not None and s["platform"] == platform]


async def run_platform(platform: str, source_ids: list[int] | None = None) -> list[SourceRunResult]:
    """Обход одной платформы: сначала выгрузка всех чатов, потом разбор ИИ.
    None → все включённые чаты платформы; явный список — только чаты этой платформы из него."""
    async with _platform_lock(platform):
        sources = _load_sources(platform, source_ids)
        if not sources:
            return []
        results: list[SourceRunResult] = []
        # VK: при остановленных диалогах не начинаем, после первого «не пускает» — не продолжаем,
        # иначе обход долбит VK по разу на источник и ловит проверку hash429 на весь аккаунт.
        vk_stop = _vk_sales_halt() if platform == "vk" else None
        vk_fetched_any = False

        try:
            async with _tg_client(platform) as client:
                for source in sources:
                    result = SourceRunResult(source["id"], source["url"], 0, 0, platform=platform)
                    results.append(result)
                    if platform == "vk":
                        result.skipped = vk_stop or _vk_skip_reason(source)
                        if result.skipped:
                            continue
                        if vk_fetched_any:
                            await asyncio.sleep(random.uniform(
                                CONFIG.vk_crawl_pause_min, max(CONFIG.vk_crawl_pause_min, CONFIG.vk_crawl_pause_max)))
                        vk_fetched_any = True
                    try:
                        result.fetched = await _fetch(source, client) or 0
                    except Exception as exc:
                        logger.exception("fetch failed for source %s", source["id"])
                        result.error = f"выгрузка: {exc}"
                        if platform == "vk" and _vk_access_lost(exc):
                            vk_stop = "обход остановлен: VK не пускает аккаунт (см. ошибку выше)"
                            logger.warning("VK: %s", vk_stop)
        except Exception as exc:
            # Не удалось даже подключиться к Telegram — выгрузка всех оставшихся чатов не состоялась.
            logger.exception("%s: обход не начался", platform)
            done = {r.source_id for r in results}
            for source in sources:
                if source["id"] not in done:
                    results.append(SourceRunResult(source["id"], source["url"], 0, 0,
                                                   error=f"подключение: {exc}", platform=platform))

        # Разбираем и чаты с ошибкой выгрузки: там могли остаться неразобранные сообщения прошлых обходов.
        for result in results:
            if not result.skipped:
                await _analyze(result)
        return results


async def run_source(source_id: int) -> SourceRunResult:
    """Один чат (кнопка в CRM/меню). Ждёт, если по его платформе уже идёт обход."""
    with db.session() as conn:
        catcher_db.migrate(conn)
        source = catcher_db.get_source(conn, source_id)
    if source is None:
        raise ValueError(f"source {source_id} not found")
    results = await run_platform(source["platform"], [source_id])
    return results[0]


async def run_sources(source_ids: list[int] | None = None,
                      platforms: tuple[str, ...] = PLATFORMS) -> list[SourceRunResult]:
    """Платформы обходятся параллельно, чаты внутри платформы — по очереди."""
    per_platform = await asyncio.gather(*(run_platform(p, source_ids) for p in platforms))
    return [r for results in per_platform for r in results]
