import sqlite3

import catcher_db
import catcher_pipeline


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    catcher_db.migrate(conn)
    return conn


def test_migrate_is_idempotent():
    conn = _mem_conn()
    catcher_db.migrate(conn)  # second call must not raise
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"source_chats", "raw_messages", "catch_candidates"} <= tables


def test_insert_raw_message_deduplicates():
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    row_id_1 = catcher_db.insert_raw_message(conn, source["id"], "42", "ivan", "hello", None, None)
    row_id_2 = catcher_db.insert_raw_message(conn, source["id"], "42", "ivan", "hello", None, None)
    assert row_id_1 is not None
    assert row_id_2 is None  # уже видели этот external_id - дубликат не создаётся


def test_cursor_only_moves_forward_on_update():
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "vk", "https://vk.com/test_group")
    assert source["last_message_id"] is None
    catcher_db.update_cursor(conn, source["id"], "100")
    updated = catcher_db.get_source(conn, source["id"])
    assert updated["last_message_id"] == "100"


def test_mark_contacted_changes_status():
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    row_id = catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу дистанционную школу", None, None)
    catcher_db.add_candidate(conn, row_id, "ищу дистанционную школу", "L3 поиск", None, "заготовка")
    candidate = catcher_db.list_candidates(conn)[0]
    assert candidate["status"] == "new"
    catcher_db.mark_contacted(conn, candidate["id"])
    updated = catcher_db.list_candidates(conn)[0]
    assert updated["status"] == "contacted"


def test_heuristic_fallback_matches_signal_words(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    messages = [
        {"author": "ivan", "text": "хотим перевести ребёнка на дистант", "url": "https://t.me/g/1"},
        {"author": "petya", "text": "продам гараж недорого", "url": "https://t.me/g/2"},
    ]
    candidates = catcher_pipeline.run_p1_on_batch(messages)
    assert len(candidates) == 1
    assert "дистант" in candidates[0]["quote"]


def test_process_source_skips_quotes_not_in_batch(monkeypatch):
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу дистанционную школу", None, None)

    def fake_run_p1(messages):
        return [{"quote": "выдуманная цитата не из батча", "reason": "x", "contact_url": None, "opener_text": "y"}]

    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", fake_run_p1)
    created = catcher_pipeline.process_source(conn, source["id"])
    assert created == 0
    assert catcher_db.list_candidates(conn) == []
