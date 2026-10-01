"""SQLite-схема и идемпотентные миграции.

Минимум PII по решению от 2026-08-12: никаких свободнотекстовых полей о ребёнке
(возраст/школа/причина ухода) в БД — только то, что нужно для скоринга/воронки/CRM.
Сырые сообщения не персистятся; итог диалога хранится как короткая нейтральная
сводка (`orders.summary`), которую пишет пайплайн, а не сырой текст лида.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import CONFIG

SCHEMA: dict[str, str] = {
    "leads": """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            source TEXT DEFAULT 'dm',
            funnel_stage INTEGER NOT NULL DEFAULT 0,
            reminder_stage INTEGER NOT NULL DEFAULT 0,
            next_step_idx INTEGER NOT NULL DEFAULT 0,
            last_touch_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            dialog_context TEXT
        )
    """,
    "orders": """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            tariff TEXT NOT NULL,
            price REAL,
            department TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'заявка',
            needs_estimator INTEGER NOT NULL DEFAULT 0,
            summary TEXT,
            handed_off_at TEXT,
            reminder_stage INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "handoff": """
        CREATE TABLE IF NOT EXISTS handoff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            score INTEGER,
            summary TEXT,
            created_at TEXT NOT NULL
        )
    """,
    "meta": """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """,
    # История переписки оператора с ТГ-агентом — переживает перезапуск процесса
    "agent_history": """
        CREATE TABLE IF NOT EXISTS agent_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """,
    # Диалоги продаж в личке VK (vk_messenger.py). Путь профиля → числовой id (разрешается один раз).
    "vk_peers": """
        CREATE TABLE IF NOT EXISTS vk_peers (
            path TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            resolved_at TEXT NOT NULL
        )
    """,
    # Что уже видели в диалоге с лидом: last_cmid — последний разобранный conversation_message_id,
    # preview — последняя строка списка диалогов (изменилась — есть что читать).
    "vk_dialogs": """
        CREATE TABLE IF NOT EXISTS vk_dialogs (
            peer_id INTEGER PRIMARY KEY,
            lead_id INTEGER REFERENCES leads(id),
            last_cmid INTEGER NOT NULL DEFAULT 0,
            preview TEXT,
            updated_at TEXT
        )
    """,
    # Что отправил сам бот: по этому списку исходящее бота отличаем от ручного сообщения
    # менеджера, и по нему же считаем лимиты отправки.
    "vk_sent": """
        CREATE TABLE IF NOT EXISTS vk_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            cmid INTEGER,
            text TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """,
    # Прогрев/напоминания VK-лидам: пишутся сюда и уходят браузером на ближайшем тике поллера.
    "vk_outbox": """
        CREATE TABLE IF NOT EXISTS vk_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            error TEXT
        )
    """,
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """,
}

# Колонки, которые могли появиться позже — для идемпотентной эволюции схемы
# (таблица -> {колонка: DDL-фрагмент типа/дефолта})
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "leads": {
        "dialog_context": "TEXT", "last_score": "INTEGER", "last_score_at": "TEXT",
        # 1 = диалог ведёт менеджер вручную («Веду сам» в ТГ-агенте): продавец не отвечает и не прогревает
        "manual_mode": "INTEGER NOT NULL DEFAULT 0",
        # id последнего обработанного входящего: догонялка после перезапуска не отвечает дважды
        "last_in_msg_id": "INTEGER NOT NULL DEFAULT 0",
        # когда лид отказался и получил вежливое завершение — второй раз не прощаемся
        "declined_at": "TEXT",
        # имя из профиля Telegram (first_name + last_name) — для карточки готового лида
        "name": "TEXT",
        # откуда лид: 'tg' или 'vk'. У VK-лида tg_id = -vk_id (колонка NOT NULL UNIQUE), см. vk_tg_id
        "platform": "TEXT NOT NULL DEFAULT 'tg'",
        "vk_id": "INTEGER", "vk_path": "TEXT",
    },
    "orders": {
        # что лид прислал в ответ на «номер и удобное время для звонка» (весь текст, как есть)
        "contact": "TEXT", "contact_at": "TEXT",
        # лента готовых лидов: куда ушла карточка ("chat:msg,..."), когда и кем передан специалисту
        "ready_msgs": "TEXT", "passed_at": "TEXT", "passed_by": "TEXT",
        # Megabitra: id лида там и строка результата для карточки («лид #123 принят» / причина)
        "megabitra_id": "TEXT", "megabitra_result": "TEXT",
    },
    "handoff": {},
    "meta": {},
    "agent_history": {},
    "vk_peers": {}, "vk_dialogs": {}, "vk_sent": {}, "vk_outbox": {},
    "users": {},
}

ORDER_STATUSES = [
    "заявка",
    "пробный_день",
    "документы",
    "договор",
    "оплачено",
    "зачислен",
]

STATUS_LABELS = {
    "заявка": "Заявка",
    "пробный_день": "Пробный день",
    "документы": "Документы",
    "договор": "Договор",
    "оплачено": "Оплачено",
    "зачислен": "Зачислен",
}

HOT_SCORE = 80


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or CONFIG.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    for table, ddl in SCHEMA.items():
        conn.execute(ddl)
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl_fragment in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_fragment}")
    conn.commit()


@contextmanager
def session(db_path: str | None = None):
    conn = connect(db_path)
    try:
        migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def reset(conn: sqlite3.Connection) -> None:
    for table in ("handoff", "orders", "leads", "meta"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


# --- leads -----------------------------------------------------------------

def get_or_create_lead(conn: sqlite3.Connection, tg_id: int, username: str | None, source: str = "dm") -> sqlite3.Row:
    row = conn.execute("SELECT * FROM leads WHERE tg_id = ?", (tg_id,)).fetchone()
    if row:
        conn.execute("UPDATE leads SET last_seen_at = ?, username = ? WHERE id = ?", (now(), username, row["id"]))
        return conn.execute("SELECT * FROM leads WHERE id = ?", (row["id"],)).fetchone()
    ts = now()
    cur = conn.execute(
        "INSERT INTO leads (tg_id, username, source, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
        (tg_id, username, source, ts, ts),
    )
    return conn.execute("SELECT * FROM leads WHERE id = ?", (cur.lastrowid,)).fetchone()



def vk_tg_id(vk_id: int) -> int:
    """Лид из VK хранится в той же таблице leads: tg_id обязателен и уникален, поэтому для VK
    это отрицательный id пользователя VK (у людей в Telegram id положительные)."""
    return -abs(int(vk_id))


def is_vk_lead(lead) -> bool:
    try:
        return lead["platform"] == "vk"
    except (IndexError, KeyError):
        return False


def vk_profile_url(lead) -> str:
    path = lead["vk_path"] or f"id{lead['vk_id']}"
    return f"https://vk.com/{path}"


def get_or_create_vk_lead(conn: sqlite3.Connection, vk_id: int, vk_path: str | None,
                          name: str | None = None, touch: bool = True) -> sqlite3.Row:
    """VK-лид по числовому id. touch=False — менеджер написал первым: last_seen_at не трогаем."""
    tg_id = vk_tg_id(vk_id)
    row = conn.execute("SELECT * FROM leads WHERE tg_id = ?", (tg_id,)).fetchone()
    ts = now()
    if row is None:
        cur = conn.execute(
            """INSERT INTO leads (tg_id, username, source, first_seen_at, last_seen_at, platform, vk_id, vk_path)
               VALUES (?, NULL, 'dm', ?, ?, 'vk', ?, ?)""",
            (tg_id, ts, ts, int(vk_id), vk_path),
        )
        row_id = cur.lastrowid
    else:
        row_id = row["id"]
        if touch:
            conn.execute("UPDATE leads SET last_seen_at = ? WHERE id = ?", (ts, row_id))
        if vk_path:
            conn.execute("UPDATE leads SET vk_path = ? WHERE id = ?", (vk_path, row_id))
    set_lead_name(conn, row_id, name)
    return conn.execute("SELECT * FROM leads WHERE id = ?", (row_id,)).fetchone()


def display_name(user) -> str | None:
    """Имя из профиля Telegram (User или Chat): «Имя Фамилия», пустое — None."""
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(p.strip() for p in parts if p and p.strip())
    return name or None


def set_lead_name(conn: sqlite3.Connection, lead_id: int, name: str | None) -> None:
    if name:
        conn.execute("UPDATE leads SET name = ? WHERE id = ?", (name, lead_id))


def leads_without_name(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM leads WHERE name IS NULL OR name = ''").fetchall()


def ensure_lead(conn: sqlite3.Connection, tg_id: int, username: str | None) -> sqlite3.Row:
    """Лид, которому менеджер написал первым: заводим карточку, но last_seen_at не трогаем —
    это время последнего сообщения ОТ лида."""
    row = conn.execute("SELECT * FROM leads WHERE tg_id = ?", (tg_id,)).fetchone()
    if row:
        return row
    return get_or_create_lead(conn, tg_id, username, source="dm")


def claim_incoming_message(conn: sqlite3.Connection, lead_id: int, msg_id: int) -> bool:
    """True, если это входящее ещё не обрабатывали (id сообщений в личке растут монотонно)."""
    cur = conn.execute(
        "UPDATE leads SET last_in_msg_id = ? WHERE id = ? AND last_in_msg_id < ?", (msg_id, lead_id, msg_id)
    )
    return cur.rowcount == 1


def mark_declined(conn: sqlite3.Connection, lead_id: int) -> None:
    """Лид отказался: запоминаем и останавливаем прогрев (next_step_idx=3 — последний шаг пройден)."""
    conn.execute("UPDATE leads SET declined_at = ?, next_step_idx = 3 WHERE id = ?", (now(), lead_id))


def set_manual_mode(conn: sqlite3.Connection, lead_id: int, on: bool) -> None:
    conn.execute("UPDATE leads SET manual_mode = ? WHERE id = ?", (1 if on else 0, lead_id))


# Сколько последних реплик держим в dialog_context (не вся история, см. SCORING_SYSTEM_PROMPT:
# "не помнить между вызовами" - это скользящее окно контекста, а не картотека).
DIALOG_CONTEXT_MAX_TURNS = 12


def append_dialog_context(conn: sqlite3.Connection, lead_id: int, role: str, text: str) -> str:
    """Добавляет реплику в dialog_context лида, обрезая до последних DIALOG_CONTEXT_MAX_TURNS. Возвращает новый контекст."""
    row = conn.execute("SELECT dialog_context FROM leads WHERE id = ?", (lead_id,)).fetchone()
    lines = (row["dialog_context"] or "").split("\n") if row and row["dialog_context"] else []
    lines = [l for l in lines if l]
    lines.append(f"{role}: {text}")
    lines = lines[-DIALOG_CONTEXT_MAX_TURNS:]
    context = "\n".join(lines)
    conn.execute("UPDATE leads SET dialog_context = ? WHERE id = ?", (context, lead_id))
    return context


def set_lead_score(conn: sqlite3.Connection, lead_id: int, score: int) -> None:
    conn.execute("UPDATE leads SET last_score = ?, last_score_at = ? WHERE id = ?", (score, now(), lead_id))


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def find_lead(conn: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    """По @username, username или Telegram-ID."""
    q = query.strip().lstrip("@")
    if q.isdigit():
        row = conn.execute("SELECT * FROM leads WHERE tg_id = ? OR id = ?", (int(q), int(q))).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM leads WHERE lower(username) = lower(?)", (q,)).fetchone()


def list_hot_leads(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM leads WHERE blocked = 0 AND last_score >= ? ORDER BY last_score_at DESC LIMIT ?",
        (HOT_SCORE, limit),
    ).fetchall()


def list_recent_leads(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM leads ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()


def block_lead(conn: sqlite3.Connection, lead_id: int, reason: str) -> None:
    conn.execute("UPDATE leads SET blocked = 1, block_reason = ? WHERE id = ?", (reason, lead_id))


def unblock_lead(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute("UPDATE leads SET blocked = 0, block_reason = NULL WHERE id = ?", (lead_id,))


def list_blocked_leads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM leads WHERE blocked = 1 ORDER BY last_seen_at DESC").fetchall()


def set_funnel_stage(conn: sqlite3.Connection, lead_id: int, stage: int) -> None:
    conn.execute("UPDATE leads SET funnel_stage = ? WHERE id = ?", (stage, lead_id))


def record_touch(conn: sqlite3.Connection, lead_id: int, next_step_idx: int) -> None:
    conn.execute(
        "UPDATE leads SET last_touch_at = ?, next_step_idx = ?, reminder_stage = 0 WHERE id = ?",
        (now(), next_step_idx, lead_id),
    )


def reset_warmup_cycle(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute("UPDATE leads SET next_step_idx = 0, reminder_stage = 0 WHERE id = ?", (lead_id,))


def active_leads_for_warmup(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM leads
        WHERE blocked = 0
          AND manual_mode = 0
          AND next_step_idx < 3
          AND id NOT IN (SELECT lead_id FROM orders WHERE closed = 0)
        """
    ).fetchall()


# --- orders ------------------------------------------------------------------

def create_order(
    conn: sqlite3.Connection,
    lead_id: int,
    tariff: str,
    price: float | None,
    department: str,
    needs_estimator: bool,
    summary: str,
) -> sqlite3.Row:
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO orders (lead_id, tariff, price, department, status, needs_estimator, summary, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'заявка', ?, ?, ?, ?)
        """,
        (lead_id, tariff, price, department, int(needs_estimator), summary, ts, ts),
    )
    return conn.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def get_order_owned_by(conn: sqlite3.Connection, order_id: int, tg_id: int) -> sqlite3.Row | None:
    """Заявка только для её владельца: чужой tg_id получает None, как будто заявки нет."""
    return conn.execute(
        "SELECT orders.* FROM orders JOIN leads ON leads.id = orders.lead_id WHERE orders.id = ? AND leads.tg_id = ?",
        (order_id, tg_id),
    ).fetchone()


def latest_order_for_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM orders WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1", (lead_id,)
    ).fetchone()


def order_awaiting_contact(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    """Открытая заявка, по которой бот попросил номер для звонка, а лид его ещё не прислал."""
    return conn.execute(
        """SELECT * FROM orders WHERE lead_id = ? AND closed = 0 AND status = 'заявка' AND contact IS NULL
           ORDER BY created_at DESC LIMIT 1""",
        (lead_id,),
    ).fetchone()


def set_order_contact(conn: sqlite3.Connection, order_id: int, contact: str) -> None:
    conn.execute(
        "UPDATE orders SET contact = ?, contact_at = ?, updated_at = ? WHERE id = ?",
        (contact, now(), now(), order_id),
    )


def set_ready_msgs(conn: sqlite3.Connection, order_id: int, msgs: list[tuple[int, int]]) -> None:
    conn.execute(
        "UPDATE orders SET ready_msgs = ? WHERE id = ?",
        (",".join(f"{chat}:{msg}" for chat, msg in msgs), order_id),
    )


def set_megabitra_result(conn: sqlite3.Connection, order_id: int, megabitra_id, text: str) -> None:
    conn.execute(
        "UPDATE orders SET megabitra_id = ?, megabitra_result = ? WHERE id = ?",
        (str(megabitra_id) if megabitra_id else None, text, order_id),
    )


def mark_passed_to_specialist(conn: sqlite3.Connection, order_id: int, by: str) -> bool:
    """False — уже был отмечен (второй оператор нажал кнопку на своей копии карточки)."""
    cur = conn.execute(
        "UPDATE orders SET passed_at = ?, passed_by = ?, updated_at = ? WHERE id = ? AND passed_at IS NULL",
        (now(), by, now(), order_id),
    )
    return cur.rowcount > 0


def ready_orders_not_passed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM orders WHERE contact IS NOT NULL AND passed_at IS NULL AND closed = 0 ORDER BY contact_at"
    ).fetchall()


def mark_handed_off(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute(
        "UPDATE orders SET status = 'документы', handed_off_at = ?, updated_at = ? WHERE id = ?",
        (now(), now(), order_id),
    )


def advance_status(conn: sqlite3.Connection, order_id: int) -> str:
    order = get_order(conn, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    idx = ORDER_STATUSES.index(order["status"])
    new_status = ORDER_STATUSES[min(idx + 1, len(ORDER_STATUSES) - 1)]
    closed = 1 if new_status == "зачислен" else order["closed"]
    conn.execute(
        "UPDATE orders SET status = ?, closed = ?, updated_at = ? WHERE id = ?",
        (new_status, closed, now(), order_id),
    )
    return new_status


def close_order(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute("UPDATE orders SET closed = 1, updated_at = ? WHERE id = ?", (now(), order_id))


def list_open_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT orders.*, leads.tg_id AS lead_tg_id, leads.username AS lead_username
        FROM orders
        JOIN leads ON leads.id = orders.lead_id
        WHERE orders.closed = 0
        ORDER BY orders.created_at DESC
        """
    ).fetchall()


def list_open_orders_by_status(conn: sqlite3.Connection, status: str, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT orders.*, leads.tg_id AS lead_tg_id, leads.username AS lead_username
        FROM orders
        JOIN leads ON leads.id = orders.lead_id
        WHERE orders.closed = 0 AND orders.status = ?
        ORDER BY orders.created_at DESC
        LIMIT ?
        """,
        (status, limit),
    ).fetchall()


def get_order_with_lead(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT orders.*, leads.tg_id AS lead_tg_id, leads.username AS lead_username
        FROM orders JOIN leads ON leads.id = orders.lead_id
        WHERE orders.id = ?
        """,
        (order_id,),
    ).fetchone()


def next_status(status: str) -> str | None:
    idx = ORDER_STATUSES.index(status)
    return ORDER_STATUSES[idx + 1] if idx + 1 < len(ORDER_STATUSES) else None


def list_orders_for_reminders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM orders WHERE closed = 0 AND status = 'заявка' AND contact IS NULL"
    ).fetchall()


def bump_order_reminder(conn: sqlite3.Connection, order_id: int, stage: int) -> None:
    conn.execute("UPDATE orders SET reminder_stage = ?, updated_at = ? WHERE id = ?", (stage, now(), order_id))


# --- handoff -----------------------------------------------------------------

def create_handoff(conn: sqlite3.Connection, order_id: int, lead_id: int, score: int | None, summary: str) -> None:
    conn.execute(
        "INSERT INTO handoff (order_id, lead_id, score, summary, created_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, lead_id, score, summary, now()),
    )


# --- meta ----------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --- agent_history ---------------------------------------------------------------

def add_agent_message(conn: sqlite3.Connection, chat_id: int, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO agent_history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, now()),
    )


def load_agent_history(conn: sqlite3.Connection, chat_id: int, limit: int) -> list[dict]:
    """Последние `limit` реплик в хронологическом порядке, приведённые к виду, который
    принимает API: начинается с user, роли чередуются (подряд идущие склеиваются)."""
    rows = conn.execute(
        "SELECT role, content FROM agent_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    messages: list[dict] = []
    for r in reversed(rows):
        if not messages and r["role"] != "user":
            continue
        if messages and messages[-1]["role"] == r["role"]:
            messages[-1]["content"] += "\n\n" + r["content"]
        else:
            messages.append({"role": r["role"], "content": r["content"]})
    return messages


def clear_agent_history(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM agent_history WHERE chat_id = ?", (chat_id,))


# --- users -----------------------------------------------------------------

def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_user(conn: sqlite3.Connection, username: str, password_hash: str) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, password_hash, now()),
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()


def set_user_password(conn: sqlite3.Connection, username: str, password_hash: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))


def delete_user(conn: sqlite3.Connection, username: str) -> None:
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
