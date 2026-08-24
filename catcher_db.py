"""Схема и CRUD для автовыгрузки чатов (lead-catcher-auto), см. .scratch/lead-catcher-auto/spec.md.

Отдельные таблицы в той же SQLite-базе, что и боевой бот, но не связаны с leads/orders —
это разовый ручной инструмент шага 1, не часть автоматического конвейера 2-8.
"""
from __future__ import annotations

import sqlite3

from db import now

SCHEMA: dict[str, str] = {
    "source_chats": """
        CREATE TABLE IF NOT EXISTS source_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL CHECK (platform IN ('tg', 'vk')),
            url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_message_id TEXT,
            last_checked_at TEXT
        )
    """,
    "raw_messages": """
        CREATE TABLE IF NOT EXISTS raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id INTEGER NOT NULL REFERENCES source_chats(id),
            external_id TEXT NOT NULL,
            author TEXT,
            text TEXT NOT NULL,
            url TEXT,
            posted_at TEXT,
            fetched_at TEXT NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0,
            author_username TEXT,
            author_tg_id INTEGER,
            UNIQUE(source_chat_id, external_id)
        )
    """,
    "catch_candidates": """
        CREATE TABLE IF NOT EXISTS catch_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
            quote TEXT NOT NULL,
            reason TEXT NOT NULL,
            contact_url TEXT,
            opener_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted')),
            created_at TEXT NOT NULL
        )
    """,
}


# Колонки, которые могли появиться позже - идемпотентная эволюция схемы (см. db.py:ADDITIVE_COLUMNS)
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "raw_messages": {
        "processed": "INTEGER NOT NULL DEFAULT 0",
        "author_username": "TEXT",
        "author_tg_id": "INTEGER",
    },
}


def migrate(conn: sqlite3.Connection) -> None:
    for ddl in SCHEMA.values():
        conn.execute(ddl)
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl_fragment in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_fragment}")
    conn.commit()


# --- source_chats -----------------------------------------------------------

def add_source(conn: sqlite3.Connection, platform: str, url: str) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO source_chats (platform, url) VALUES (?, ?)", (platform, url)
    )
    return conn.execute("SELECT * FROM source_chats WHERE id = ?", (cur.lastrowid,)).fetchone()


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source_chats ORDER BY id").fetchall()


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_chats WHERE id = ?", (source_id,)).fetchone()


def set_source_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    conn.execute("UPDATE source_chats SET enabled = ? WHERE id = ?", (int(enabled), source_id))


def update_cursor(conn: sqlite3.Connection, source_id: int, last_message_id: str) -> None:
    conn.execute(
        "UPDATE source_chats SET last_message_id = ?, last_checked_at = ? WHERE id = ?",
        (last_message_id, now(), source_id),
    )


# --- raw_messages -------------------------------------------------------------

def insert_raw_message(
    conn: sqlite3.Connection,
    source_chat_id: int,
    external_id: str,
    author: str | None,
    text: str,
    url: str | None,
    posted_at: str | None,
    author_username: str | None = None,
    author_tg_id: int | None = None,
) -> int | None:
    """Возвращает id вставленной строки, либо None если сообщение уже видели (дубликат)."""
    try:
        cur = conn.execute(
            """
            INSERT INTO raw_messages
                (source_chat_id, external_id, author, text, url, posted_at, fetched_at, author_username, author_tg_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_chat_id, external_id, author, text, url, posted_at, now(), author_username, author_tg_id),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def unprocessed_messages_for_source(conn: sqlite3.Connection, source_chat_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM raw_messages WHERE source_chat_id = ? AND processed = 0 ORDER BY id", (source_chat_id,)
    ).fetchall()


def mark_messages_processed(conn: sqlite3.Connection, message_ids: list[int]) -> None:
    if not message_ids:
        return
    placeholders = ",".join("?" * len(message_ids))
    conn.execute(f"UPDATE raw_messages SET processed = 1 WHERE id IN ({placeholders})", message_ids)


# --- catch_candidates ----------------------------------------------------------

def add_candidate(
    conn: sqlite3.Connection,
    raw_message_id: int,
    quote: str,
    reason: str,
    contact_url: str | None,
    opener_text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO catch_candidates (raw_message_id, quote, reason, contact_url, opener_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (raw_message_id, quote, reason, contact_url, opener_text, now()),
    )


def list_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT catch_candidates.*,
               raw_messages.url AS message_url,
               raw_messages.author AS author_name,
               raw_messages.author_username AS author_username,
               raw_messages.author_tg_id AS author_tg_id
        FROM catch_candidates
        JOIN raw_messages ON raw_messages.id = catch_candidates.raw_message_id
        ORDER BY catch_candidates.created_at DESC
        """
    ).fetchall()


def mark_contacted(conn: sqlite3.Connection, candidate_id: int) -> None:
    conn.execute("UPDATE catch_candidates SET status = 'contacted' WHERE id = ?", (candidate_id,))
