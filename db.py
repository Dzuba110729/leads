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
    "leads": {"dialog_context": "TEXT"},
    "orders": {},
    "handoff": {},
    "meta": {},
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


def block_lead(conn: sqlite3.Connection, lead_id: int, reason: str) -> None:
    conn.execute("UPDATE leads SET blocked = 1, block_reason = ? WHERE id = ?", (reason, lead_id))


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


def latest_order_for_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM orders WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1", (lead_id,)
    ).fetchone()


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


def list_orders_for_reminders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM orders WHERE closed = 0 AND status = 'заявка'"
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
