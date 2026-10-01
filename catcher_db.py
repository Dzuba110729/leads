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
            reply_to_external_id TEXT,
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
            confidence TEXT NOT NULL DEFAULT 'high',
            created_at TEXT NOT NULL,
            UNIQUE(raw_message_id)
        )
    """,
}


# Колонки, которые могли появиться позже - идемпотентная эволюция схемы (см. db.py:ADDITIVE_COLUMNS)
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # Запомненный Telegram-peer: без него каждый обход заново ищет чат по @username
    # (ResolveUsernameRequest), а у этого запроса жёсткий суточный лимит -> FloodWait на часы.
    "source_chats": {
        "tg_peer_type": "TEXT",
        "tg_peer_id": "INTEGER",
        "tg_access_hash": "INTEGER",
        # Название чата (для бесед VK — вместо ссылки vk.com/im/convo/… в списках и отчётах)
        "title": "TEXT",
    },
    "raw_messages": {
        "processed": "INTEGER NOT NULL DEFAULT 0",
        "author_username": "TEXT",
        "author_tg_id": "INTEGER",
        "reply_to_external_id": "TEXT",
    },
    "catch_candidates": {
        "confidence": "TEXT NOT NULL DEFAULT 'high'",
    },
}

# UNIQUE(raw_message_id) появился позже CREATE TABLE, поэтому для уже созданных баз
# его добавляем отдельным индексом - иначе перескан наплодит дубли одних и тех же лидов.
EXTRA_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_raw_message ON catch_candidates(raw_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_raw_messages_source_processed ON raw_messages(source_chat_id, processed)",
)


def migrate(conn: sqlite3.Connection) -> None:
    for ddl in SCHEMA.values():
        conn.execute(ddl)
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl_fragment in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_fragment}")
    for ddl in EXTRA_INDEXES:
        conn.execute(ddl)
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


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Удаляет чат вместе с выгруженными сообщениями и найденными в нём кандидатами."""
    conn.execute(
        "DELETE FROM catch_candidates WHERE raw_message_id IN (SELECT id FROM raw_messages WHERE source_chat_id = ?)",
        (source_id,),
    )
    conn.execute("DELETE FROM raw_messages WHERE source_chat_id = ?", (source_id,))
    conn.execute("DELETE FROM source_chats WHERE id = ?", (source_id,))


def mark_checked(conn: sqlite3.Connection, source_id: int) -> None:
    """Время обхода без сдвига курсора: VK-источник мог отдать 0 новых, а обход всё равно был."""
    conn.execute("UPDATE source_chats SET last_checked_at = ? WHERE id = ?", (now(), source_id))


def update_cursor(conn: sqlite3.Connection, source_id: int, last_message_id: str) -> None:
    conn.execute(
        "UPDATE source_chats SET last_message_id = ?, last_checked_at = ? WHERE id = ?",
        (last_message_id, now(), source_id),
    )


def save_tg_peer(conn: sqlite3.Connection, source_id: int, peer_type: str, peer_id: int, access_hash: int | None) -> None:
    conn.execute(
        "UPDATE source_chats SET tg_peer_type = ?, tg_peer_id = ?, tg_access_hash = ? WHERE id = ?",
        (peer_type, peer_id, access_hash, source_id),
    )
    conn.commit()


def set_source_title(conn: sqlite3.Connection, source_id: int, title: str) -> None:
    conn.execute("UPDATE source_chats SET title = ? WHERE id = ?", (title, source_id))


def source_label(source) -> str:
    """Как назвать чат оператору: название, если знаем, иначе ссылка."""
    title = _field(source, "title") or _field(source, "source_title")
    url = _field(source, "url") or _field(source, "source_url") or ""
    return title or url


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
    reply_to_external_id: str | None = None,
) -> int | None:
    """Возвращает id вставленной строки, либо None если сообщение уже видели (дубликат)."""
    try:
        cur = conn.execute(
            """
            INSERT INTO raw_messages
                (source_chat_id, external_id, author, text, url, posted_at, fetched_at,
                 author_username, author_tg_id, reply_to_external_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_chat_id, external_id, author, text, url, posted_at, now(),
             author_username, author_tg_id, reply_to_external_id),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def recent_messages(conn: sqlite3.Connection, source_chat_id: int, limit: int) -> list[sqlite3.Row]:
    """Последние сохранённые сообщения чата — в них ищем, на что отвечают в беседе VK."""
    return conn.execute(
        "SELECT external_id, author, text FROM raw_messages WHERE source_chat_id = ? ORDER BY id DESC LIMIT ?",
        (source_chat_id, limit),
    ).fetchall()


def unprocessed_messages_for_source(conn: sqlite3.Connection, source_chat_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM raw_messages WHERE source_chat_id = ? AND processed = 0 ORDER BY id", (source_chat_id,)
    ).fetchall()


def reply_targets(conn: sqlite3.Connection, source_chat_id: int, external_ids: list[str]) -> dict[str, sqlite3.Row]:
    """Сообщения, на которые отвечают разбираемые: без них ответ вида «да, у нас та же беда»
    читается как бессмыслица и лид теряется."""
    if not external_ids:
        return {}
    placeholders = ",".join("?" * len(external_ids))
    rows = conn.execute(
        f"SELECT * FROM raw_messages WHERE source_chat_id = ? AND external_id IN ({placeholders})",
        [source_chat_id, *external_ids],
    ).fetchall()
    return {r["external_id"]: r for r in rows}


def reset_processed_for_source(conn: sqlite3.Connection, source_chat_id: int, fetched_before: str | None = None) -> int:
    """Снимает пометку «разобрано», чтобы сообщения прошли через П1 заново.
    `fetched_before` ограничивает переразбор старыми выгрузками — их читал код, который
    потом чинили именно из-за потери лидов. Возвращает число затронутых сообщений."""
    if fetched_before:
        cur = conn.execute(
            "UPDATE raw_messages SET processed = 0 WHERE source_chat_id = ? AND processed = 1 AND fetched_at < ?",
            (source_chat_id, fetched_before),
        )
    else:
        cur = conn.execute(
            "UPDATE raw_messages SET processed = 0 WHERE source_chat_id = ? AND processed = 1", (source_chat_id,)
        )
    return cur.rowcount


def source_stats(conn: sqlite3.Connection, source_chat_id: int) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END) AS pending
        FROM raw_messages WHERE source_chat_id = ?
        """,
        (source_chat_id,),
    ).fetchone()


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
    confidence: str = "high",
) -> bool:
    """Возвращает False, если кандидат на это сообщение уже был — пачки идут внахлёст
    и прогонов несколько, поэтому одно сообщение легко находится дважды."""
    try:
        conn.execute(
            """
            INSERT INTO catch_candidates
                (raw_message_id, quote, reason, contact_url, opener_text, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (raw_message_id, quote, reason, contact_url, opener_text, confidence, now()),
        )
        return True
    except sqlite3.IntegrityError:
        # Уже найден раньше. Уверенную находку не понижаем до «под вопросом», но повышаем наоборот.
        if confidence == "high":
            conn.execute(
                "UPDATE catch_candidates SET confidence = 'high' WHERE raw_message_id = ? AND confidence != 'high'",
                (raw_message_id,),
            )
        return False


CANDIDATES_PAGE_SIZE = 50

# Показываем только точных кандидатов. Старые «под вопросом» остаются в базе, но нигде не видны.
VISIBLE_SQL = "catch_candidates.confidence = 'high'"


# Когда автор написал сообщение, в сравнимом виде: TG и браузерный VK хранят ISO-дату,
# VK через API — unix-время строкой.
POSTED_AT_SQL = """CASE WHEN raw_messages.posted_at GLOB '[0-9]*' AND raw_messages.posted_at NOT LIKE '%-%'
    THEN datetime(raw_messages.posted_at, 'unixepoch') ELSE datetime(raw_messages.posted_at) END"""


def list_candidates(
    conn: sqlite3.Connection,
    status: str | None = None,
    confidence: str | None = None,
    limit: int = CANDIDATES_PAGE_SIZE,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where, params = [VISIBLE_SQL], []
    if status:
        where.append("catch_candidates.status = ?")
        params.append(status)
    if confidence:
        where.append("catch_candidates.confidence = ?")
        params.append(confidence)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"""
        SELECT catch_candidates.*,
               raw_messages.url AS message_url,
               raw_messages.author AS author_name,
               raw_messages.author_username AS author_username,
               raw_messages.author_tg_id AS author_tg_id,
               raw_messages.posted_at AS posted_at,
               source_chats.platform AS source_platform
        FROM catch_candidates
        JOIN raw_messages ON raw_messages.id = catch_candidates.raw_message_id
        JOIN source_chats ON source_chats.id = raw_messages.source_chat_id
        {clause}
        ORDER BY {POSTED_AT_SQL} IS NULL, {POSTED_AT_SQL} DESC, catch_candidates.created_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()


def list_candidates_for_export(
    conn: sqlite3.Connection,
    status: str | None = "new",
    source_ids: list[int] | None = None,
    since: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Кандидаты с чатом-источником и датой сообщения — для отчёта агента."""
    where, params = [VISIBLE_SQL], []
    if status:
        where.append("catch_candidates.status = ?")
        params.append(status)
    if source_ids:
        where.append(f"raw_messages.source_chat_id IN ({','.join('?' * len(source_ids))})")
        params.extend(source_ids)
    if since:
        where.append("catch_candidates.created_at >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"""
        SELECT catch_candidates.*,
               raw_messages.url AS message_url,
               raw_messages.text AS message_text,
               raw_messages.posted_at AS posted_at,
               raw_messages.author AS author_name,
               raw_messages.author_username AS author_username,
               raw_messages.author_tg_id AS author_tg_id,
               source_chats.url AS source_url,
               source_chats.title AS source_title,
               source_chats.platform AS source_platform
        FROM catch_candidates
        JOIN raw_messages ON raw_messages.id = catch_candidates.raw_message_id
        JOIN source_chats ON source_chats.id = raw_messages.source_chat_id
        {clause}
        ORDER BY source_chats.url, catch_candidates.confidence = 'maybe', catch_candidates.created_at DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()


def count_candidates(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS new,
               SUM(CASE WHEN status = 'new' AND confidence = 'high' THEN 1 ELSE 0 END) AS new_high,
               SUM(CASE WHEN status = 'new' AND confidence = 'maybe' THEN 1 ELSE 0 END) AS new_maybe
        FROM catch_candidates
        WHERE confidence = 'high'
        """
    ).fetchone()
    return {k: (row[k] or 0) for k in ("total", "new", "new_high", "new_maybe")}


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT catch_candidates.*,
               raw_messages.url AS message_url,
               raw_messages.author AS author_name,
               raw_messages.author_username AS author_username,
               raw_messages.author_tg_id AS author_tg_id,
               source_chats.platform AS source_platform
        FROM catch_candidates
        JOIN raw_messages ON raw_messages.id = catch_candidates.raw_message_id
        JOIN source_chats ON source_chats.id = raw_messages.source_chat_id
        WHERE catch_candidates.id = ?
        """,
        (candidate_id,),
    ).fetchone()


def mark_contacted(conn: sqlite3.Connection, candidate_id: int) -> None:
    conn.execute("UPDATE catch_candidates SET status = 'contacted' WHERE id = ?", (candidate_id,))


# --- контакт автора ----------------------------------------------------------------------------

def _field(row, name):
    """Поле и из sqlite3.Row, и из dict (в тестах и старых выборках части колонок нет)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def author_label(row) -> str:
    """Как подписать автора: в Telegram — @username, в VK — имя (короткое имя там не «ник»)."""
    username, name = _field(row, "author_username"), _field(row, "author_name")
    if _field(row, "source_platform") == "vk":
        return name or (f"vk.com/{username}" if username else "автор неизвестен")
    return f"@{username}" if username else (name or "автор неизвестен")


def author_link(row) -> str | None:
    """Ссылка на личку автора — только из выгруженных данных, не из ответа модели (П1 контакты
    не выдумывает, но и полагаться на это незачем): TG — t.me/<username> или tg://user,
    VK — vk.com/<id… или короткое имя>."""
    username, tg_id = _field(row, "author_username"), _field(row, "author_tg_id")
    if _field(row, "source_platform") == "vk":
        return f"https://vk.com/{username}" if username else None
    if username:
        return f"https://t.me/{username}"
    if tg_id:
        return f"tg://user?id={tg_id}"
    return None
