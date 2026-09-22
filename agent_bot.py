"""ТГ-агент оператора: отдельный бот, понимает свободный текст, отвечает на вопросы по проекту
и запускает обход чатов ловца лидов с отчётом в Google Docs.

Слушает только Telegram-ID из AGENT_ALLOWED_IDS. Долгие задачи (обход чатов) уходят в фон:
агент сразу отвечает «запустил», а по завершении сам присылает итог и ссылку на документ.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.types import Message

import catcher_db
import catcher_service
import db
import gdocs
from config import CONFIG

logger = logging.getLogger(__name__)

agent_dp = Dispatcher()

MAX_HISTORY_TURNS = 20
MAX_TOOL_ROUNDS = 8
TELEGRAM_MESSAGE_LIMIT = 4000

_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY_TURNS * 2))
_running_jobs: dict[int, asyncio.Task] = {}
_client = None


def _anthropic():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.AsyncAnthropic(api_key=CONFIG.llm_api_key)
    return _client


def _project_docs() -> str:
    parts = []
    for name in ("README.md", "CLAUDE.md"):
        path = Path(__file__).with_name(name)
        if path.exists():
            parts.append(f"===== {name} =====\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


SYSTEM_PROMPT = """Ты — ассистент оператора системы og1 (Telegram-бот продаж + ловец лидов + веб-CRM).
Общаешься с владельцем системы в Telegram. Он не программист: отвечай коротко, простыми словами,
без технического жаргона, по-русски. Не используй markdown-заголовки и таблицы — Telegram их не рендерит;
допустимы короткие списки с «—».

Что ты умеешь через инструменты:
- отвечать на вопросы о том, как устроен проект (описание ниже) и что сейчас в базе (get_stats, list_recent_candidates);
- запускать обход чатов ловца лидов: start_catcher_run. Обход идёт в фоне несколько минут; после вызова
  инструмента скажи, что запустил и что пришлёшь итог и ссылку на документ сам, как только всё закончится.
  Не проси пользователя ждать в чате и не обещай точное время;
- собирать документ Google Docs со всеми найденными лидами: export_leads. Если пользователь просит «обойти и прислать
  документ» — вызывай start_catcher_run с export_to_gdoc=true, а не два инструмента подряд.

Как понимать «обойти чат X»: сначала list_sources, найди источник по совпадению названия/ссылки, передай его id.
«Все чаты» → source_ids пустой список. Если источник не найден — так и скажи, перечисли доступные.
Если инструмент вернул ошибку — честно перескажи её одной фразой и предложи, что делать.

Описание проекта (для ответов на вопросы):

"""

TOOLS = [
    {
        "name": "list_sources",
        "description": "Список подключённых чатов-источников ловца лидов: id, платформа, ссылка, включён ли, сколько сообщений выгружено и ждут разбора.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "start_catcher_run",
        "description": "Запустить в фоне обход чатов: выгрузить новые сообщения и найти среди них лидов. Возвращает сразу, итог придёт пользователю отдельным сообщением.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "id источников из list_sources. Пустой список = все включённые.",
                },
                "export_to_gdoc": {
                    "type": "boolean",
                    "description": "После обхода собрать Google-документ с новыми лидами и прислать ссылку.",
                },
            },
            "required": ["source_ids", "export_to_gdoc"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "export_leads",
        "description": "Собрать Google-документ с найденными лидами (цитата, повод, вариант ответа, ссылки в чат и в личку) и вернуть ссылку.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["new", "contacted", "all"],
                    "description": "new — кому ещё не писали (по умолчанию), contacted — кому уже написали, all — все.",
                },
                "source_ids": {"type": "array", "items": {"type": "integer"}, "description": "Пустой список = все чаты."},
                "since_days": {"type": "integer", "description": "Только найденные за последние N дней. 0 = без ограничения."},
            },
            "required": ["status", "source_ids", "since_days"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_stats",
        "description": "Сводка по базе: лиды продаж, заявки по статусам, заблокированные, кандидаты ловца (новые/под вопросом/написал).",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "list_recent_candidates",
        "description": "Последние найденные ловцом кандидаты коротким текстом: кто, из какого чата, цитата, статус.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Сколько показать, 1-20."},
                "status": {"type": "string", "enum": ["new", "contacted", "all"]},
            },
            "required": ["limit", "status"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


# --- инструменты -------------------------------------------------------------------

def _tool_list_sources() -> str:
    with db.session() as conn:
        catcher_db.migrate(conn)
        rows = []
        for s in catcher_db.list_sources(conn):
            stats = catcher_db.source_stats(conn, s["id"])
            rows.append({
                "id": s["id"],
                "platform": s["platform"],
                "url": s["url"],
                "enabled": bool(s["enabled"]),
                "messages_total": stats["total"] or 0,
                "messages_pending": stats["pending"] or 0,
                "last_checked_at": s["last_checked_at"],
            })
    return json.dumps(rows, ensure_ascii=False)


def _tool_get_stats() -> str:
    with db.session() as conn:
        catcher_db.migrate(conn)
        leads = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        blocked = conn.execute("SELECT COUNT(*) n FROM leads WHERE blocked = 1").fetchone()["n"]
        orders = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) n FROM orders WHERE closed = 0 GROUP BY status")
        }
        counts = catcher_db.count_candidates(conn)
        contacted = conn.execute("SELECT COUNT(*) n FROM catch_candidates WHERE status = 'contacted'").fetchone()["n"]
        sources = conn.execute("SELECT COUNT(*) n FROM source_chats WHERE enabled = 1").fetchone()["n"]
    return json.dumps({
        "sales_leads_total": leads,
        "sales_leads_blocked": blocked,
        "open_orders_by_status": orders,
        "catcher_sources_enabled": sources,
        "catcher_candidates": {**counts, "contacted": contacted},
    }, ensure_ascii=False)


def _tool_list_recent_candidates(limit: int, status: str) -> str:
    limit = max(1, min(20, int(limit)))
    with db.session() as conn:
        catcher_db.migrate(conn)
        rows = catcher_db.list_candidates_for_export(conn, status=None if status == "all" else status, limit=limit)
    out = []
    for c in rows:
        out.append({
            "author": c["author_username"] and f"@{c['author_username']}" or c["author_name"],
            "chat": c["source_url"],
            "quote": c["quote"],
            "reason": c["reason"],
            "status": c["status"],
            "confidence": c["confidence"],
            "message_url": c["message_url"],
        })
    return json.dumps(out, ensure_ascii=False)


def _export_leads_sync(status: str, source_ids: list[int], since_days: int, title_suffix: str = "") -> tuple[str, int]:
    if not gdocs.available():
        raise RuntimeError("Google Docs не настроен: запустите google_login.py")
    since = None
    if since_days and since_days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    with db.session() as conn:
        catcher_db.migrate(conn)
        rows = catcher_db.list_candidates_for_export(
            conn, status=None if status == "all" else status, source_ids=source_ids or None, since=since
        )
    url = gdocs.create_leads_document(rows, title_suffix)
    return url, len(rows)


async def _tool_export_leads(status: str, source_ids: list[int], since_days: int) -> str:
    url, count = await asyncio.to_thread(_export_leads_sync, status, source_ids, since_days)
    return json.dumps({"url": url, "leads_in_document": count}, ensure_ascii=False)


async def _catcher_job(bot: Bot, chat_id: int, source_ids: list[int], export: bool) -> None:
    try:
        results = await catcher_service.run_sources(source_ids or None)
        lines = ["Обход закончен."]
        total_new = 0
        for r in results:
            name = r.url.replace("https://", "")
            if r.error:
                lines.append(f"— {name}: ошибка ({r.error})")
            else:
                lines.append(f"— {name}: выгружено {r.fetched}, новых лидов {r.new_candidates}")
                total_new += r.new_candidates
        if export:
            try:
                url, count = await asyncio.to_thread(
                    _export_leads_sync, "new", source_ids, 0, " (после обхода)"
                )
                lines.append(f"\nДокумент с лидами, кому ещё не писали ({count}): {url}")
            except Exception as exc:
                logger.exception("export after catcher run failed")
                lines.append(f"\nДокумент собрать не удалось: {exc}")
        elif total_new:
            lines.append("\nСкажите «собери документ», если нужен отчёт в Google Docs.")
        await _send_long(bot, chat_id, "\n".join(lines))
    except Exception as exc:
        logger.exception("catcher job failed")
        await _send_long(bot, chat_id, f"Обход прервался с ошибкой: {exc}")
    finally:
        _running_jobs.pop(chat_id, None)


def _tool_start_catcher_run(bot: Bot, chat_id: int, source_ids: list[int], export: bool) -> str:
    if chat_id in _running_jobs and not _running_jobs[chat_id].done():
        return json.dumps({"error": "обход уже идёт, дождитесь его завершения"}, ensure_ascii=False)
    if export and not gdocs.available():
        return json.dumps({"error": "Google Docs не настроен (нужен google_login.py), обход не запущен"}, ensure_ascii=False)
    _running_jobs[chat_id] = asyncio.create_task(_catcher_job(bot, chat_id, source_ids, export))
    return json.dumps({"started": True, "sources": source_ids or "all", "export_to_gdoc": export}, ensure_ascii=False)


async def _execute_tool(name: str, args: dict, bot: Bot, chat_id: int) -> tuple[str, bool]:
    try:
        if name == "list_sources":
            return _tool_list_sources(), False
        if name == "get_stats":
            return _tool_get_stats(), False
        if name == "list_recent_candidates":
            return _tool_list_recent_candidates(args["limit"], args["status"]), False
        if name == "export_leads":
            return await _tool_export_leads(args["status"], args["source_ids"], args["since_days"]), False
        if name == "start_catcher_run":
            return _tool_start_catcher_run(bot, chat_id, args["source_ids"], args["export_to_gdoc"]), False
        return f"неизвестный инструмент {name}", True
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return f"Ошибка: {exc}", True


# --- диалог с моделью ------------------------------------------------------------------

async def answer(bot: Bot, chat_id: int, user_text: str) -> str:
    history = _history[chat_id]
    history.append({"role": "user", "content": user_text})
    messages = list(history)
    system = [{"type": "text", "text": SYSTEM_PROMPT + _project_docs(), "cache_control": {"type": "ephemeral"}}]
    client = _anthropic()

    final_text = ""
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.messages.create(
            model=CONFIG.agent_model,
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        text_blocks = [b.text for b in response.content if b.type == "text"]
        if response.stop_reason == "refusal":
            final_text = "Не могу ответить на это."
            break
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            final_text = "\n".join(text_blocks).strip()
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for tu in tool_uses:
            content, is_error = await _execute_tool(tu.name, tu.input, bot, chat_id)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    else:
        final_text = "Слишком много шагов подряд, остановился. Попробуйте переформулировать."

    if not final_text:
        final_text = "Готово."
    history.append({"role": "assistant", "content": final_text})
    return final_text


async def _send_long(bot: Bot, chat_id: int, text: str) -> None:
    for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        await bot.send_message(chat_id, text[i : i + TELEGRAM_MESSAGE_LIMIT], disable_web_page_preview=True)


def _allowed(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in CONFIG.agent_allowed_ids


@agent_dp.message(CommandStart())
async def start(message: Message) -> None:
    if not _allowed(message):
        await message.answer(
            f"Этот бот только для оператора og1. Ваш Telegram-ID: {message.from_user.id} — "
            "добавьте его в AGENT_ALLOWED_IDS в .env, чтобы получить доступ."
        )
        return
    await message.answer(
        "Я агент og1. Спрашивайте про проект или командуйте: «обойди все чаты и пришли документ», "
        "«сколько новых лидов», «покажи последних 5»."
    )


@agent_dp.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    if not _allowed(message):
        logger.warning("agent: ignored message from %s", message.from_user.id if message.from_user else None)
        return
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        reply = await answer(bot, message.chat.id, message.text)
    except Exception as exc:
        logger.exception("agent answer failed")
        reply = f"Не получилось обработать запрос: {exc}"
    await _send_long(bot, message.chat.id, reply)


def build_agent_bot() -> Bot:
    return Bot(token=CONFIG.agent_bot_token)
