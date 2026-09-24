"""Меню ТГ-агента: постоянные кнопки разделов внизу чата + инлайн-кнопки внутри разделов.

Разделы: Сводка, Лиды, Ловец, Заявки, Система, Помощь. Всё, что не нажатие кнопки, уходит
в свободный диалог с моделью (agent_bot.chat_router). Действия, которые что-то меняют
(смена статуса заявки, боевой режим, напоминания), всегда идут через подтверждение.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import catcher_db
import db
import gdocs
import runtime
from config import CONFIG, save_env_value

logger = logging.getLogger(__name__)

router = Router(name="agent_menu")

BTN_SUMMARY = "📊 Сводка"
BTN_LEADS = "🔥 Лиды"
BTN_CATCHER = "🎣 Ловец"
BTN_ORDERS = "📋 Заявки"
BTN_SYSTEM = "⚙️ Система"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = {BTN_SUMMARY, BTN_LEADS, BTN_CATCHER, BTN_ORDERS, BTN_SYSTEM, BTN_HELP}

LIST_LIMIT = 10
DIALOG_LINES_IN_CARD = 8
DIALOG_LINE_MAX = 300

BOT_COMMANDS = [
    BotCommand(command="menu", description="Показать меню"),
    BotCommand(command="summary", description="Сводка за сегодня"),
    BotCommand(command="new", description="Начать разговор заново"),
    BotCommand(command="help", description="Что умеет бот"),
]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUMMARY), KeyboardButton(text=BTN_LEADS)],
            [KeyboardButton(text=BTN_CATCHER), KeyboardButton(text=BTN_ORDERS)],
            [KeyboardButton(text=BTN_SYSTEM), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел или напишите вопрос",
    )


def allowed(user_id: int | None) -> bool:
    return user_id is not None and user_id in CONFIG.agent_allowed_ids


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _fmt_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m %H:%M")
    except ValueError:
        return value


def _today_start_utc() -> str:
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat()


def _score_badge(score: int | None) -> str:
    if score is None:
        return "—"
    if score >= db.HOT_SCORE:
        return f"🔥 {score}"
    if score >= 50:
        return f"🟠 {score}"
    if score >= 20:
        return f"🟡 {score}"
    return f"⚪ {score}"


def _lead_name(lead) -> str:
    return f"@{lead['username']}" if lead["username"] else f"ID {lead['tg_id']}"


def _status_label(status: str) -> str:
    return db.STATUS_LABELS.get(status, status)


def _price(price) -> str:
    return f"{price:,.0f} ₽".replace(",", " ") if price else "по запросу"


# --- Сводка -----------------------------------------------------------------------------

def summary_data() -> dict:
    since = _today_start_utc()
    with db.session() as conn:
        catcher_db.migrate(conn)
        one = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0  # noqa: E731
        data = {
            "new_leads": one("SELECT COUNT(*) FROM leads WHERE first_seen_at >= ?", since),
            "active_leads": one("SELECT COUNT(*) FROM leads WHERE last_seen_at >= ?", since),
            "hot_today": one(
                "SELECT COUNT(*) FROM leads WHERE last_score >= ? AND last_score_at >= ?", db.HOT_SCORE, since
            ),
            "new_orders": one("SELECT COUNT(*) FROM orders WHERE created_at >= ?", since),
            "blocked": one("SELECT COUNT(*) FROM leads WHERE blocked = 1"),
            "candidates_today": one("SELECT COUNT(*) FROM catch_candidates WHERE created_at >= ?", since),
            "candidates_new": catcher_db.count_candidates(conn)["new"],
            "open_orders": {
                r["status"]: r["n"]
                for r in conn.execute("SELECT status, COUNT(*) n FROM orders WHERE closed = 0 GROUP BY status")
            },
        }
    return data


def render_summary() -> tuple[str, InlineKeyboardMarkup]:
    d = summary_data()
    orders = ", ".join(f"{_status_label(s)} — {n}" for s, n in d["open_orders"].items()) or "нет"
    text = (
        f"<b>📊 Сводка за сегодня</b> ({datetime.now().strftime('%d.%m')})\n\n"
        f"<b>Продажи</b>\n"
        f"• Новых людей написало: {d['new_leads']}\n"
        f"• Диалогов сегодня: {d['active_leads']}\n"
        f"• Горячих (80+): {d['hot_today']}\n"
        f"• Новых заявок: {d['new_orders']}\n"
        f"• Открытые заявки: {_e(orders)}\n\n"
        f"<b>Ловец лидов</b>\n"
        f"• Найдено сегодня: {d['candidates_today']}\n"
        f"• Ждут, чтобы вы написали: {d['candidates_new']}\n"
    )
    if d["blocked"]:
        text += f"\n⛔ Заблокировано защитой: {d['blocked']}"
    kb = _kb(
        [_btn("🔥 Горячие", "leads:hot"), _btn("🆕 Кандидаты", "cat:cand:0")],
        [_btn("📋 Заявки", "menu:orders"), _btn("🔄 Обновить", "menu:summary")],
    )
    return text, kb


# --- Лиды -------------------------------------------------------------------------------

def render_leads_menu() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "<b>🔥 Лиды</b> — люди, которые пишут продающему аккаунту.\n\n"
        "Балл 0–100 — насколько человек готов к покупке: 🔥 80+ горячий (бот передаёт менеджеру), "
        "🟠 50–79, 🟡 20–49 бот прогревает, ⚪ до 20 бот молчит."
    )
    kb = _kb(
        [_btn("🔥 Горячие (80+)", "leads:hot")],
        [_btn("💬 Последние диалоги", "leads:recent")],
        [_btn("⛔ Заблокированные", "leads:blocked")],
    )
    return text, kb


def render_leads_list(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    with db.session() as conn:
        if kind == "hot":
            rows, title, empty = db.list_hot_leads(conn, LIST_LIMIT), "🔥 Горячие лиды", "Горячих лидов пока нет."
        elif kind == "blocked":
            rows, title, empty = (
                db.list_blocked_leads(conn)[:LIST_LIMIT], "⛔ Заблокированные",
                "Заблокированных нет — защита ни на кого не срабатывала.",
            )
        else:
            rows, title, empty = db.list_recent_leads(conn, LIST_LIMIT), "💬 Последние диалоги", "Пока никто не писал."
    if not rows:
        return f"<b>{title}</b>\n\n{empty}", _kb([_btn("« Лиды", "menu:leads")])
    text = f"<b>{title}</b>\nНажмите на человека, чтобы открыть карточку."
    buttons = [
        [_btn(f"{_lead_name(l)} · {_score_badge(l['last_score'])} · {_fmt_dt(l['last_seen_at'])}", f"lead:{l['id']}")]
        for l in rows
    ]
    buttons.append([_btn("« Лиды", "menu:leads")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def lead_details(lead_id: int) -> dict | None:
    with db.session() as conn:
        lead = db.get_lead(conn, lead_id)
        if lead is None:
            return None
        order = db.latest_order_for_lead(conn, lead_id)
    lines = [l for l in (lead["dialog_context"] or "").split("\n") if l][-DIALOG_LINES_IN_CARD:]
    return {"lead": lead, "order": order, "dialog": lines}


def render_lead_card(lead_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    info = lead_details(lead_id)
    if info is None:
        return "Лид не найден.", _kb([_btn("« Лиды", "menu:leads")])
    lead, order = info["lead"], info["order"]
    source = "группа" if lead["source"] == "group" else "личка"
    parts = [
        f"<b>{_e(_lead_name(lead))}</b>",
        f"Балл: {_score_badge(lead['last_score'])} (оценён {_fmt_dt(lead['last_score_at'])})",
        f"Откуда: {source} · шаг прогрева: {lead['next_step_idx']}/3",
        f"Первый контакт: {_fmt_dt(lead['first_seen_at'])} · последнее сообщение: {_fmt_dt(lead['last_seen_at'])}",
    ]
    if not lead["username"]:
        parts.append(f"Telegram-ID: <code>{lead['tg_id']}</code> (у человека нет username — найдите его в чатах аккаунта)")
    if order is not None:
        closed = " (закрыта)" if order["closed"] else ""
        parts.append(f"Заявка #{order['id']}: {_e(order['tariff'])}, {_status_label(order['status'])}{closed}")
    if lead["blocked"]:
        parts.append(f"⛔ Заблокирован: {_e(lead['block_reason'] or 'без причины')}")
    if info["dialog"]:
        parts.append("\n<b>Последние сообщения</b>")
        for line in info["dialog"]:
            short = line if len(line) <= DIALOG_LINE_MAX else line[:DIALOG_LINE_MAX] + "…"
            parts.append(f"— {_e(short)}")
    if note:
        parts.insert(0, note + "\n")
    rows = []
    if lead["username"]:
        rows.append([_url_btn("✉️ Открыть в Telegram", f"https://t.me/{lead['username']}")])
    if order is not None:
        rows.append([_btn(f"📋 Заявка #{order['id']}", f"ord:{order['id']}")])
    if lead["blocked"]:
        rows.append([_btn("🔓 Разблокировать", f"lead_unblock:{lead['id']}")])
    rows.append([_btn("« Лиды", "menu:leads")])
    return "\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows)


# --- Ловец ------------------------------------------------------------------------------

# Чаты, от которых ждём ссылку после нажатия «➕ Добавить чат» (живёт до перезапуска — этого хватает)
_awaiting_source: set[int] = set()


def parse_source_link(text: str) -> tuple[str, str] | None:
    """Ссылка/упоминание группы → (платформа, нормализованная ссылка на сам чат) или None.
    Ссылку на сообщение (t.me/chat/123) сводим к чату; приглашения (t.me/+..., joinchat) не
    поддерживаем — по ним служебный аккаунт не прочитает чат, пока в него не вступит."""
    raw = text.strip().split()[0].strip(",;()<>\"'«»") if text.strip() else ""
    if raw.startswith("@") and len(raw) > 1:
        return "tg", f"https://t.me/{raw[1:]}"
    link = raw.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    for prefix in ("t.me/", "telegram.me/"):
        if link.startswith(prefix):
            name = link[len(prefix):].split("/")[0].split("?")[0]
            if not name or name.startswith("+") or name in ("joinchat", "c", "s"):
                return None
            return "tg", f"https://t.me/{name}"
    for prefix in ("vk.com/", "m.vk.com/"):
        if link.startswith(prefix):
            name = link[len(prefix):].split("/")[0].split("?")[0]
            return ("vk", f"https://vk.com/{name}") if name else None
    return None


def _looks_like_link(token: str) -> bool:
    return token.startswith("@") or any(m in token for m in ("t.me/", "telegram.me/", "vk.com/", "http"))


def add_sources_from_text(text: str) -> dict[str, list[str]]:
    """Все ссылки из сообщения (через пробел, запятую или с новой строки) → добавлены / уже были /
    не распознаны. Обычные слова между ссылками («Чат родителей: ...», нумерация) пропускаем."""
    result: dict[str, list[str]] = {"added": [], "duplicates": [], "invalid": []}
    tokens = [t for t in re.split(r"[\s,;]+", text) if t]
    with db.session() as conn:
        catcher_db.migrate(conn)
        known = {s["url"].rstrip("/").lower() for s in catcher_db.list_sources(conn)}
        for token in tokens:
            if not _looks_like_link(token):
                continue
            parsed = parse_source_link(token)
            if parsed is None:
                result["invalid"].append(token)
                continue
            platform, url = parsed
            if url.lower() in known:
                result["duplicates"].append(url)
                continue
            catcher_db.add_source(conn, platform, url)
            known.add(url.lower())
            result["added"].append(url)
    return result


def describe_added(result: dict[str, list[str]]) -> str:
    parts = []
    if result["added"]:
        parts.append(f"✅ Добавлено чатов: {len(result['added'])}")
    if result["duplicates"]:
        parts.append("Уже были подключены: " + ", ".join(result["duplicates"]))
    if result["invalid"]:
        parts.append(
            "Не получилось добавить: " + ", ".join(result["invalid"])
            + "\n(нужна ссылка на открытую группу вида https://t.me/название; ссылки-приглашения t.me/+... не подходят)"
        )
    return "\n".join(parts) or "Не нашёл в сообщении ни одной ссылки на чат."


def add_source_from_text(text: str) -> tuple[bool, str]:
    result = add_sources_from_text(text)
    return bool(result["added"]), describe_added(result)


def render_catcher_menu() -> tuple[str, InlineKeyboardMarkup]:
    with db.session() as conn:
        catcher_db.migrate(conn)
        counts = catcher_db.count_candidates(conn)
        sources = [s for s in catcher_db.list_sources(conn) if s["enabled"]]
    text = (
        "<b>🎣 Ловец лидов</b>\n\n"
        f"Подключено чатов: {len(sources)}\n"
        f"Кандидатов, кому ещё не писали: {counts['new']}"
        + (f" (из них под вопросом: {counts['new_maybe']})" if counts["new_maybe"] else "")
        + "\n\nДобавить чат или поставить на паузу — кнопка «📚 Чаты»."
    )
    kb = _kb(
        [_btn("📄 Обойти и прислать документ", "cat:rundoc")],
        [_btn("▶️ Обойти все чаты", "cat:runall"), _btn("🎯 Обойти один", "cat:pick")],
        [_btn(f"🎯 Только точные ({counts['new_high']})", "cat:cand:0:h"),
         _btn(f"🆕 Все ({counts['new']})", "cat:cand:0")],
        [_btn("📄 Собрать документ", "cat:doc"), _btn("📚 Чаты", "cat:sources")],
    )
    return text, kb


def render_sources(pick: bool) -> tuple[str, InlineKeyboardMarkup]:
    with db.session() as conn:
        catcher_db.migrate(conn)
        sources = catcher_db.list_sources(conn)
        stats = {s["id"]: catcher_db.source_stats(conn, s["id"]) for s in sources}
    if not sources:
        return (
            "Чатов пока нет. Нажмите «➕ Добавить чат» и пришлите ссылку на открытую группу.",
            _kb([_btn("➕ Добавить чат", "cat:add")], [_btn("« Ловец", "menu:catcher")]),
        )
    if pick:
        rows = [
            [_btn(f"{'TG' if s['platform'] == 'tg' else 'VK'} · {s['url'].replace('https://', '')[:40]}", f"cat:run:{s['id']}")]
            for s in sources if s["enabled"]
        ]
        rows.append([_btn("« Ловец", "menu:catcher")])
        return "<b>🎯 Какой чат обойти?</b>", InlineKeyboardMarkup(inline_keyboard=rows)
    lines = ["<b>📚 Подключённые чаты</b>\n"]
    rows = []
    for n, s in enumerate(sources, 1):
        st = stats[s["id"]]
        state = "✅" if s["enabled"] else "⏸ на паузе"
        lines.append(
            f"{n}. {state} {_e(s['url'])}\n    сообщений: {st['total'] or 0}, проверен: {_fmt_dt(s['last_checked_at'])}"
        )
        rows.append([_btn(
            f"{n}. {'⏸ Поставить на паузу' if s['enabled'] else '▶️ Включить'}",
            f"cat:toggle:{s['id']}",
        )])
    rows.append([_btn("➕ Добавить чат", "cat:add")])
    rows.append([_btn("« Ловец", "menu:catcher")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_candidate(offset: int, note: str = "", only_high: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка одного нового кандидата. only_high — листать только точных (без «под вопросом»)."""
    mode = ":h" if only_high else ""
    with db.session() as conn:
        catcher_db.migrate(conn)
        counts = catcher_db.count_candidates(conn)
        total = counts["new_high"] if only_high else counts["new"]
        offset = max(0, min(offset, max(total - 1, 0)))
        rows = catcher_db.list_candidates(
            conn, status="new", confidence="high" if only_high else None, limit=1, offset=offset
        )
    if not rows:
        if only_high and counts["new"]:
            empty = f"Точных кандидатов не осталось. Под вопросом ещё {counts['new_maybe']} — их можно посмотреть во всех."
            kb = _kb([_btn(f"🆕 Все кандидаты ({counts['new']})", "cat:cand:0")], [_btn("« Ловец", "menu:catcher")])
        else:
            empty = "Новых кандидатов нет — все разобраны. Запустите обход чатов, чтобы найти ещё."
            kb = _kb([_btn("▶️ Обойти все чаты", "cat:runall")], [_btn("« Ловец", "menu:catcher")])
        return (note + "\n\n" if note else "") + empty, kb
    c = rows[0]
    author = f"@{c['author_username']}" if c["author_username"] else (c["author_name"] or "автор неизвестен")
    maybe = " · <i>под вопросом</i>" if c["confidence"] == "maybe" else ""
    title = "Точный кандидат" if only_high else "Кандидат"
    text = (
        (note + "\n\n" if note else "")
        + f"<b>{title} {offset + 1} из {total}</b>{maybe}\n"
        f"<b>{_e(author)}</b>\n\n"
        f"<b>Что написал:</b> «{_e(c['quote'])}»\n\n"
        f"<b>Почему это лид:</b> {_e(c['reason'])}\n\n"
        f"<b>Вариант первого сообщения</b> (нажмите, чтобы скопировать):\n<code>{_e(c['opener_text'])}</code>"
    )
    links = []
    if c["message_url"]:
        links.append(_url_btn("💬 Сообщение в чате", c["message_url"]))
    if c["author_username"]:
        links.append(_url_btn("👤 Написать в личку", f"https://t.me/{c['author_username']}"))
    nav = [_btn("✅ Написал", f"cat:done:{c['id']}:{offset}{mode}")]
    if offset + 1 < total:
        nav.append(_btn("⏭ Дальше", f"cat:cand:{offset + 1}{mode}"))
    back = []
    if offset > 0:
        back.append(_btn("⏮ Назад", f"cat:cand:{offset - 1}{mode}"))
    back.append(_btn("« Ловец", "menu:catcher"))
    return text, _kb(links, nav, back)


def render_doc_menu() -> tuple[str, InlineKeyboardMarkup]:
    if not gdocs.available():
        return (
            "Google Docs не подключён — см. инструкцию docs/google-docs-setup.md в папке проекта.",
            _kb([_btn("« Ловец", "menu:catcher")]),
        )
    return "<b>📄 Какой документ собрать?</b>", _kb(
        [_btn("Кому ещё не писали", "cat:mkdoc:new:0")],
        [_btn("Все за 7 дней", "cat:mkdoc:all:7"), _btn("Все за 30 дней", "cat:mkdoc:all:30")],
        [_btn("« Ловец", "menu:catcher")],
    )


# --- Заявки -----------------------------------------------------------------------------

def render_orders_menu() -> tuple[str, InlineKeyboardMarkup]:
    with db.session() as conn:
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) n FROM orders WHERE closed = 0 GROUP BY status")
        }
    total = sum(counts.values())
    text = f"<b>📋 Заявки</b>\n\nОткрытых заявок: {total}. Выберите этап:"
    rows = [
        [_btn(f"{_status_label(s)} ({counts.get(s, 0)})", f"ord:list:{i}")]
        for i, s in enumerate(db.ORDER_STATUSES) if s != "зачислен"
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_orders_list(status_idx: int) -> tuple[str, InlineKeyboardMarkup]:
    status = db.ORDER_STATUSES[status_idx]
    with db.session() as conn:
        rows = db.list_open_orders_by_status(conn, status, LIST_LIMIT)
    if not rows:
        return f"<b>{_status_label(status)}</b>\n\nНа этом этапе заявок нет.", _kb([_btn("« Заявки", "menu:orders")])
    buttons = [
        [_btn(
            f"#{o['id']} · {'@' + o['lead_username'] if o['lead_username'] else 'ID ' + str(o['lead_tg_id'])} · {o['tariff'][:25]}",
            f"ord:{o['id']}",
        )]
        for o in rows
    ]
    buttons.append([_btn("« Заявки", "menu:orders")])
    return f"<b>{_status_label(status)}</b> — нажмите на заявку:", InlineKeyboardMarkup(inline_keyboard=buttons)


def render_order_card(order_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    with db.session() as conn:
        o = db.get_order_with_lead(conn, order_id)
    if o is None:
        return "Заявка не найдена.", _kb([_btn("« Заявки", "menu:orders")])
    who = f"@{o['lead_username']}" if o["lead_username"] else f"ID {o['lead_tg_id']}"
    parts = [
        f"<b>Заявка #{o['id']}</b>",
        f"Кто: {_e(who)}",
        f"Тариф: {_e(o['tariff'])}",
        f"Стоимость: {_price(o['price'])}",
        f"Этап: <b>{_status_label(o['status'])}</b>" + (" (закрыта)" if o["closed"] else ""),
        f"Создана: {_fmt_dt(o['created_at'])} · обновлена: {_fmt_dt(o['updated_at'])}",
    ]
    if o["needs_estimator"]:
        parts.append("⚠️ Цену нужно поставить вручную")
    if o["summary"]:
        parts.append(f"\n<b>Суть:</b> {_e(o['summary'])}")
    if note:
        parts.insert(0, note + "\n")
    rows = []
    nxt = db.next_status(o["status"])
    if nxt and not o["closed"]:
        rows.append([_btn(f"➡️ Перевести в «{_status_label(nxt)}»", f"ord:adv:{o['id']}")])
    if o["lead_username"]:
        rows.append([_url_btn("✉️ Написать в Telegram", f"https://t.me/{o['lead_username']}")])
    rows.append([_btn("👤 Карточка лида", f"lead:{o['lead_id']}"), _btn("« Заявки", "menu:orders")])
    return "\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows)


def render_order_confirm(order_id: int) -> tuple[str, InlineKeyboardMarkup]:
    with db.session() as conn:
        o = db.get_order(conn, order_id)
    if o is None or db.next_status(o["status"]) is None:
        return render_order_card(order_id)
    nxt = _status_label(db.next_status(o["status"]))
    notify = "Лиду придёт уведомление о новом статусе." if not CONFIG.dry_run else "Тестовый режим — лиду ничего не уйдёт."
    return (
        f"Перевести заявку #{order_id} из «{_status_label(o['status'])}» в «{nxt}»?\n{notify}",
        _kb([_btn("✅ Да, перевести", f"ord:advok:{order_id}"), _btn("Отмена", f"ord:{order_id}")]),
    )


# --- Система ----------------------------------------------------------------------------

def system_status() -> dict:
    userbot = runtime.userbot
    uptime = datetime.now(timezone.utc) - runtime.started_at
    return {
        "userbot_connected": bool(userbot is not None and userbot.is_connected()),
        "userbot_configured": bool(CONFIG.tg_api_id and CONFIG.tg_api_hash),
        "cashier_bot": runtime.cashier_bot_enabled,
        "llm": bool(CONFIG.llm_api_key),
        "google_docs": gdocs.available(),
        "catcher_tg": bool(CONFIG.catcher_tg_string_session),
        "vk": bool(CONFIG.vk_access_token),
        "dry_run": CONFIG.dry_run,
        "scheduler_enabled": CONFIG.scheduler_enabled,
        "group_reply_mode": CONFIG.group_reply_mode,
        "started_at": runtime.started_at.isoformat(),
        "uptime_hours": round(uptime.total_seconds() / 3600, 1),
        "crm_url": f"http://{CONFIG.crm_host}:{CONFIG.crm_port}",
    }


def render_system(note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    s = system_status()
    ok = lambda flag: "✅" if flag else "❌"  # noqa: E731
    if s["userbot_connected"]:
        userbot = "✅ Продающий аккаунт подключён"
    elif s["userbot_configured"]:
        userbot = "❌ Продающий аккаунт НЕ подключён — нужен перезапуск"
    else:
        userbot = "❌ Продающий аккаунт не настроен"
    lines = [
        "<b>⚙️ Состояние системы</b>\n",
        userbot,
        f"{ok(s['cashier_bot'])} Бот передачи заявок",
        f"{ok(s['llm'])} ИИ (Claude)",
        f"{ok(s['google_docs'])} Google Docs",
        f"{ok(s['catcher_tg'])} Ловец: Telegram · {ok(s['vk'])} VK",
        f"✅ CRM: {s['crm_url']}",
        f"Работает с {_fmt_dt(s['started_at'])} ({s['uptime_hours']} ч)\n",
        "<b>Режимы</b>",
        ("🟢 Боевой режим: ВКЛ — бот реально отвечает людям" if not s["dry_run"]
         else "⚪ Боевой режим: ВЫКЛ — бот только пишет в лог, людям ничего не уходит"),
        ("🟢 Напоминания затихшим: ВКЛ" if s["scheduler_enabled"] else "⚪ Напоминания затихшим: ВЫКЛ"),
        f"Ответы в группах: {'выкл' if s['group_reply_mode'] == 'off' else s['group_reply_mode']} (меняется в .env)",
    ]
    if note:
        lines.insert(0, note + "\n")
    kb = _kb(
        [_btn("⏸ Выключить боевой режим" if not s["dry_run"] else "▶️ Включить боевой режим",
              f"sys:dry:{'on' if not s['dry_run'] else 'off'}")],
        [_btn("⏸ Выключить напоминания" if s["scheduler_enabled"] else "▶️ Включить напоминания",
              f"sys:sch:{'off' if s['scheduler_enabled'] else 'on'}")],
        [_btn("🔄 Обновить", "menu:system")],
    )
    return "\n".join(lines), kb


# Для dry: 'on' = включить тестовый режим (DRY_RUN=1), 'off' = выключить его (боевой режим)
TOGGLES = {
    "dry": {
        "env": "DRY_RUN", "attr": "dry_run",
        "confirm": {
            "on": "Выключить боевой режим? Бот перестанет отвечать людям и будет только писать в лог.",
            "off": "Включить боевой режим? Бот начнёт РЕАЛЬНО отвечать людям в Telegram.",
        },
        "done": {"on": "⏸ Боевой режим выключен.", "off": "▶️ Боевой режим включён."},
    },
    "sch": {
        "env": "SCHEDULER_ENABLED", "attr": "scheduler_enabled",
        "confirm": {
            "on": "Включить напоминания? Бот сам будет писать затихшим лидам (через 1, 3 и 7 дней) "
                  "и напоминать о незавершённых заявках.",
            "off": "Выключить напоминания затихшим лидам?",
        },
        "done": {"on": "▶️ Напоминания включены.", "off": "⏸ Напоминания выключены."},
    },
}


def apply_toggle(kind: str, value: str) -> str:
    spec = TOGGLES[kind]
    flag = value == "on"
    setattr(CONFIG, spec["attr"], flag)
    save_env_value(spec["env"], "1" if flag else "0")
    return spec["done"][value]


HELP_TEXT = (
    "<b>❓ Что я умею</b>\n\n"
    "Кнопки внизу — разделы:\n"
    "• <b>📊 Сводка</b> — что произошло сегодня\n"
    "• <b>🔥 Лиды</b> — горячие, последние диалоги, заблокированные; карточка человека с перепиской\n"
    "• <b>🎣 Ловец</b> — обойти чаты (можно сразу с Google-документом), разобрать кандидатов, добавить чат\n"
    "• <b>📋 Заявки</b> — заявки по этапам, перевод на следующий этап\n"
    "• <b>⚙️ Система</b> — всё ли работает, боевой режим, напоминания\n\n"
    "А ещё можно просто писать обычным текстом, например:\n"
    "— кто сегодня самый горячий?\n"
    "— что бот ответил @ivan?\n"
    "— обойди все чаты и пришли документ\n"
    "— сколько заявок на этапе договора?\n\n"
    "/new — начать разговор заново (я забуду предыдущие вопросы)"
)


# --- обработчики --------------------------------------------------------------------------

async def _show(target: Message | CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    """Сообщение → новое сообщение; нажатие кнопки → правим то же сообщение."""
    text, kb = view
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def _guard(target: Message | CallbackQuery) -> bool:
    user = target.from_user
    if allowed(user.id if user else None):
        return True
    if isinstance(target, CallbackQuery):
        await target.answer("Нет доступа", show_alert=True)
    else:
        await target.answer(
            f"Этот бот только для оператора og1. Ваш Telegram-ID: {user.id if user else '?'} — "
            "добавьте его в AGENT_ALLOWED_IDS в .env, чтобы получить доступ."
        )
    return False


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message) -> None:
    if not await _guard(message):
        return
    await message.answer(
        "Я агент og1 — помощник по боту продаж и ловцу лидов.\n\n"
        "Разделы — на кнопках внизу. Или просто напишите вопрос обычным текстом.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    if await _guard(message):
        await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=main_keyboard())


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    if not await _guard(message):
        return
    with db.session() as conn:
        db.clear_agent_history(conn, message.chat.id)
    await message.answer("Начали разговор заново — прошлые вопросы я забыл.", reply_markup=main_keyboard())


@router.message(Command("summary"))
@router.message(F.text == BTN_SUMMARY)
async def btn_summary(message: Message) -> None:
    if await _guard(message):
        await _show(message, render_summary())


@router.message(F.text == BTN_LEADS)
async def btn_leads(message: Message) -> None:
    if await _guard(message):
        await _show(message, render_leads_menu())


@router.message(F.text == BTN_CATCHER)
async def btn_catcher(message: Message) -> None:
    if await _guard(message):
        await _show(message, render_catcher_menu())


@router.message(F.text == BTN_ORDERS)
async def btn_orders(message: Message) -> None:
    if await _guard(message):
        await _show(message, render_orders_menu())


@router.message(F.text == BTN_SYSTEM)
async def btn_system(message: Message) -> None:
    if await _guard(message):
        await _show(message, render_system())


MENU_VIEWS = {
    "summary": render_summary,
    "leads": render_leads_menu,
    "catcher": render_catcher_menu,
    "orders": render_orders_menu,
    "system": render_system,
}


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(cb: CallbackQuery) -> None:
    if await _guard(cb):
        view = MENU_VIEWS.get(cb.data.split(":", 1)[1])
        if view:
            await _show(cb, view())


@router.callback_query(F.data.startswith("leads:"))
async def cb_leads(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_leads_list(cb.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("lead:"))
async def cb_lead(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_lead_card(int(cb.data.split(":", 1)[1])))


@router.callback_query(F.data.startswith("lead_unblock:"))
async def cb_unblock(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    lead_id = int(cb.data.split(":", 1)[1])
    with db.session() as conn:
        db.unblock_lead(conn, lead_id)
    logger.info("agent: lead %s unblocked by %s", lead_id, cb.from_user.id)
    await _show(cb, render_lead_card(lead_id, note="🔓 Разблокирован — бот снова будет отвечать этому человеку."))


@router.callback_query(F.data == "cat:pick")
async def cb_cat_pick(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_sources(pick=True))


@router.callback_query(F.data == "cat:sources")
async def cb_cat_sources(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_sources(pick=False))


@router.callback_query(F.data == "cat:add")
async def cb_cat_add(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    _awaiting_source.add(cb.message.chat.id)
    await _show(cb, (
        "Пришлите ссылку на <b>открытую</b> группу или канал — или сразу <b>список</b> "
        "(каждая ссылка с новой строки, через пробел или запятую):\n"
        "• Telegram: https://t.me/название или @название\n"
        "• VK: https://vk.com/название\n\n"
        "Служебный аккаунт ловца должен иметь доступ к чату (для закрытых — вступить в него заранее).",
        _kb([_btn("Отмена", "cat:addcancel")]),
    ))


@router.callback_query(F.data == "cat:addcancel")
async def cb_cat_add_cancel(cb: CallbackQuery) -> None:
    if await _guard(cb):
        _awaiting_source.discard(cb.message.chat.id)
        await _show(cb, render_sources(pick=False))


@router.message(F.text, lambda m: m.chat.id in _awaiting_source and m.text not in MENU_BUTTONS and not m.text.startswith("/"))
async def on_source_link(message: Message) -> None:
    if not await _guard(message):
        return
    if parse_source_link(message.text) is None and not any(m in message.text for m in ("t.me", "vk.com", "@")):
        # Это не попытка прислать ссылку, а обычный вопрос — отдаём его свободному диалогу
        _awaiting_source.discard(message.chat.id)
        raise SkipHandler()
    result = add_sources_from_text(message.text)
    note = describe_added(result)
    if result["added"] or result["duplicates"]:
        _awaiting_source.discard(message.chat.id)
        text, kb = render_sources(pick=False)
        await message.answer(f"{_e(note)}\n\n{text}", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        await message.answer(_e(note), parse_mode="HTML", reply_markup=_kb([_btn("Отмена", "cat:addcancel")]),
                             disable_web_page_preview=True)


@router.callback_query(F.data.startswith("cat:toggle:"))
async def cb_cat_toggle(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    source_id = int(cb.data.rsplit(":", 1)[1])
    with db.session() as conn:
        source = catcher_db.get_source(conn, source_id)
        if source is not None:
            catcher_db.set_source_enabled(conn, source_id, not source["enabled"])
    await _show(cb, render_sources(pick=False))


@router.callback_query(F.data.in_({"cat:runall", "cat:rundoc"}))
@router.callback_query(F.data.startswith("cat:run:"))
async def cb_cat_run(cb: CallbackQuery, bot: Bot) -> None:
    if not await _guard(cb):
        return
    import agent_bot  # здесь, а не наверху: agent_bot сам импортирует этот модуль

    ids = [int(cb.data.rsplit(":", 1)[1])] if cb.data.startswith("cat:run:") else []
    export = cb.data == "cat:rundoc"
    result = agent_bot.start_catcher_job(bot, cb.message.chat.id, ids, export=export)
    if "error" in result:
        note = f"⚠️ {_e(result['error'])}"
    elif export:
        note = ("📄 Запустил обход всех чатов. Это займёт несколько минут — пришлю итог "
                "и ссылку на Google-документ с лидами, кому ещё не писали.")
    else:
        note = "▶️ Запустил обход. Это займёт несколько минут — итог пришлю отдельным сообщением."
    text, kb = render_catcher_menu()
    await _show(cb, (f"{note}\n\n{text}", kb))


@router.callback_query(F.data.startswith("cat:cand:"))
async def cb_cat_candidate(cb: CallbackQuery) -> None:
    if await _guard(cb):
        parts = cb.data.split(":")  # cat:cand:<offset>[:h]
        await _show(cb, render_candidate(int(parts[2]), only_high=parts[-1] == "h"))


@router.callback_query(F.data.startswith("cat:done:"))
async def cb_cat_done(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    parts = cb.data.split(":")  # cat:done:<id>:<offset>[:h]
    with db.session() as conn:
        catcher_db.mark_contacted(conn, int(parts[2]))
    # отмеченный уходит из списка «новых», поэтому на том же месте уже следующий
    await _show(cb, render_candidate(int(parts[3]), note="✅ Отметил, что вы написали.", only_high=parts[-1] == "h"))


@router.callback_query(F.data == "cat:doc")
async def cb_cat_doc(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_doc_menu())


@router.callback_query(F.data.startswith("cat:mkdoc:"))
async def cb_cat_mkdoc(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    import agent_bot

    _, _, status, days = cb.data.split(":")
    await _show(cb, ("⏳ Собираю документ…", _kb()))
    try:
        url, count = await asyncio.to_thread(agent_bot.export_leads_sync, status, [], int(days))
        text = f"📄 Документ готов, лидов в нём: {count}\n{_e(url)}"
    except Exception as exc:
        logger.exception("agent menu: export failed")
        text = f"Документ собрать не удалось: {_e(exc)}"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_kb([_btn("« Ловец", "menu:catcher")]))


@router.callback_query(F.data.startswith("ord:list:"))
async def cb_orders_list(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_orders_list(int(cb.data.rsplit(":", 1)[1])))


@router.callback_query(F.data.startswith("ord:adv:"))
async def cb_order_adv(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_order_confirm(int(cb.data.rsplit(":", 1)[1])))


@router.callback_query(F.data.startswith("ord:advok:"))
async def cb_order_advok(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    import bot as bot_module

    order_id = int(cb.data.rsplit(":", 1)[1])
    try:
        new_status = await bot_module.advance_and_notify(order_id)
        note = f"✅ Заявка переведена в «{_status_label(new_status)}»."
    except ValueError:
        note = "Заявка не найдена."
    logger.info("agent: order %s advanced by %s", order_id, cb.from_user.id)
    await _show(cb, render_order_card(order_id, note=note))


@router.callback_query(F.data.regexp(r"^ord:\d+$"))
async def cb_order(cb: CallbackQuery) -> None:
    if await _guard(cb):
        await _show(cb, render_order_card(int(cb.data.split(":", 1)[1])))


@router.callback_query(F.data.regexp(r"^sys:(dry|sch):(on|off)$"))
async def cb_sys_confirm(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    _, kind, value = cb.data.split(":")
    text = TOGGLES[kind]["confirm"][value]
    await _show(cb, (text, _kb([_btn("✅ Да", f"sys:ok:{kind}:{value}"), _btn("Отмена", "menu:system")])))


@router.callback_query(F.data.regexp(r"^sys:ok:(dry|sch):(on|off)$"))
async def cb_sys_apply(cb: CallbackQuery) -> None:
    if not await _guard(cb):
        return
    _, _, kind, value = cb.data.split(":")
    note = apply_toggle(kind, value)
    logger.warning("agent: %s set to %s by %s", TOGGLES[kind]["env"], value, cb.from_user.id)
    await _show(cb, render_system(note=note))


async def set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("agent: failed to set bot commands")
