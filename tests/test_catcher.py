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
    candidates, ok = catcher_pipeline.run_p1_on_batch(messages)
    assert len(candidates) == 1
    assert "дистант" in candidates[0]["quote"]
    assert ok is False  # модель не отработала - пачку нельзя считать разобранной


def test_process_source_skips_quotes_not_in_batch(monkeypatch):
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу дистанционную школу", None, None)

    def fake_run_p1(messages, reply_map=None):
        return [{"quote": "выдуманная цитата не из батча", "reason": "x", "contact_url": None, "opener_text": "y"}], True

    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", fake_run_p1)
    created = catcher_pipeline.process_source(conn, source["id"])
    assert created == 0
    assert catcher_db.list_candidates(conn) == []


def test_candidate_resolved_by_number_not_quote():
    """Номер сообщения - основной путь: слегка перефразированная цитата больше не теряется."""
    chunk = [{"id": 11, "text": "ищу дистанционную школу для сына"}, {"id": 22, "text": "всем привет"}]
    candidate = {"n": 1, "quote": "ищу дистанционку сыну"}  # цитата перефразирована
    assert catcher_pipeline.resolve_message_id(candidate, chunk) == 11


def test_candidate_falls_back_to_quote_without_number():
    chunk = [{"id": 11, "text": "ищу дистанционную школу для сына"}, {"id": 22, "text": "всем привет"}]
    assert catcher_pipeline.resolve_message_id({"quote": "ищу дистанционную школу"}, chunk) == 11
    assert catcher_pipeline.resolve_message_id({"quote": "продам гараж в Твери"}, chunk) is None


def test_chunks_overlap_so_split_dialogue_stays_whole():
    messages = list(range(10))
    chunks = catcher_pipeline._chunks(messages, size=4, overlap=2)
    assert all(len(c) <= 4 for c in chunks)
    assert set().union(*[set(c) for c in chunks]) == set(messages)
    # соседние пачки перекрываются - сообщение на границе попадает в обе
    assert set(chunks[0]) & set(chunks[1])


def test_failed_llm_leaves_messages_for_another_run(monkeypatch):
    """Сбой LLM не должен хоронить пачку: именно так лиды терялись до 2026-09-10."""
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу дистанционную школу", None, None)

    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, False))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", lambda m, r=None: ([], False))
    catcher_pipeline.process_source(conn, source["id"])
    assert len(catcher_db.unprocessed_messages_for_source(conn, source["id"])) == 1


def test_successful_run_marks_messages_processed(monkeypatch):
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу дистанционную школу", None, None)

    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", lambda m, r=None: ([], True))
    catcher_pipeline.process_source(conn, source["id"])
    assert catcher_db.unprocessed_messages_for_source(conn, source["id"]) == []


def test_same_message_found_twice_creates_one_candidate():
    """Пачки внахлёст и несколько проходов находят одно сообщение повторно - дубля быть не должно."""
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    row_id = catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу школу", None, None)
    assert catcher_db.add_candidate(conn, row_id, "ищу школу", "повод", None, "заход", "maybe") is True
    assert catcher_db.add_candidate(conn, row_id, "ищу школу", "повод", None, "заход", "high") is False
    candidates = catcher_db.list_candidates(conn)
    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "high"  # уверенная находка повышает спорную


def test_rescan_returns_messages_to_the_queue():
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    row_id = catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", "ищу школу", None, None)
    catcher_db.mark_messages_processed(conn, [row_id])
    assert catcher_db.unprocessed_messages_for_source(conn, source["id"]) == []
    assert catcher_db.reset_processed_for_source(conn, source["id"]) == 1
    assert len(catcher_db.unprocessed_messages_for_source(conn, source["id"])) == 1


def test_candidate_list_filters_by_status_and_confidence():
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    first = catcher_db.insert_raw_message(conn, source["id"], "1", "a", "ищу школу", None, None)
    second = catcher_db.insert_raw_message(conn, source["id"], "2", "b", "тоже ищу", None, None)
    catcher_db.add_candidate(conn, first, "ищу школу", "r", None, "o", "high")
    catcher_db.add_candidate(conn, second, "тоже ищу", "r", None, "o", "maybe")
    catcher_db.mark_contacted(conn, catcher_db.list_candidates(conn, confidence="high")[0]["id"])

    assert len(catcher_db.list_candidates(conn, status="new")) == 1
    assert len(catcher_db.list_candidates(conn, status="new", confidence="maybe")) == 1
    assert len(catcher_db.list_candidates(conn, status="contacted")) == 1
    counts = catcher_db.count_candidates(conn)
    assert counts["total"] == 2 and counts["new"] == 1 and counts["new_maybe"] == 1


def test_reply_context_lands_in_prompt():
    """Ответ в ветке без исходного сообщения читается как бессмыслица - контекст должен подставляться."""
    parent = {"id": 1, "text": "сына травят в школе, не знаю что делать", "author": "mama"}
    messages = [{"id": 2, "text": "да, у нас та же беда", "author": "papa", "reply_to_external_id": "100"}]
    rendered = catcher_pipeline._format_batch(messages, {"100": parent})
    assert "в ответ на" in rendered
    assert "травят в школе" in rendered
