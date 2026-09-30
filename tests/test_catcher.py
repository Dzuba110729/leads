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


def test_no_llm_gives_no_candidates_and_leaves_batch_unprocessed(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)  # и API недоступен (claude_code — в conftest)
    messages = [
        {"author": "ivan", "text": "хотим перевести ребёнка на дистант", "url": "https://t.me/g/1"},
        {"author": "petya", "text": "продам коврик в хорошем состоянии", "url": "https://t.me/g/2"},
    ]
    candidates, ok = catcher_pipeline.run_p1_on_batch(messages)
    assert candidates == []  # без модели ничего не выдумываем — мусорных «лидов» не будет
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


def _run_with_p1_answer(monkeypatch, text: str, answer: dict) -> list:
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "ivan", text, None, None)
    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", lambda m, r=None: ([{"n": 1, **answer}], True))
    catcher_pipeline.process_source(conn, source["id"])
    return conn.execute("SELECT * FROM catch_candidates").fetchall()


def test_only_confident_candidates_are_saved(monkeypatch):
    assert _run_with_p1_answer(monkeypatch, "может школу сменить", {"confidence": "maybe"}) == []
    assert len(_run_with_p1_answer(monkeypatch, "ищу дистанционную школу", {"confidence": "high"})) == 1


def test_ukraine_is_not_a_lead(monkeypatch):
    # Модель сама пометила семью из Украины.
    assert _run_with_p1_answer(
        monkeypatch, "мы из Харькова, ищем школу", {"confidence": "high", "from_ukraine": True}
    ) == []
    # Модель флаг забыла, но сообщение по-украински — всё равно отсекаем.
    assert _run_with_p1_answer(monkeypatch, "шукаємо онлайн-школу для доньки", {"confidence": "high"}) == []


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

    # «Под вопросом» остаётся в базе, но нигде не показывается и не считается.
    assert catcher_db.list_candidates(conn, status="new") == []
    assert catcher_db.list_candidates(conn, status="new", confidence="maybe") == []
    assert len(catcher_db.list_candidates(conn, status="contacted")) == 1
    counts = catcher_db.count_candidates(conn)
    assert counts["total"] == 1 and counts["new"] == 0


def test_reply_context_lands_in_prompt():
    """Ответ в ветке без исходного сообщения читается как бессмыслица - контекст должен подставляться."""
    parent = {"id": 1, "text": "сына травят в школе, не знаю что делать", "author": "mama"}
    messages = [{"id": 2, "text": "да, у нас та же беда", "author": "papa", "reply_to_external_id": "100"}]
    rendered = catcher_pipeline._format_batch(messages, {"100": parent})
    assert "в ответ на" in rendered
    assert "травят в школе" in rendered


def test_message_link_formats():
    from types import SimpleNamespace

    import catcher_tg

    public = SimpleNamespace(username="parents_chat", id=1, megagroup=True)
    assert catcher_tg.message_link(public, 5, "x") == "https://t.me/parents_chat/5"
    private = SimpleNamespace(username=None, id=2223334445, megagroup=True, broadcast=False)
    assert catcher_tg.message_link(private, 93751, "x") == "https://t.me/c/2223334445/93751"
    small = SimpleNamespace(username=None, id=77)
    assert catcher_tg.message_link(small, 1, "https://t.me/+abc") == "https://t.me/+abc"


def test_candidates_sorted_by_message_date_newest_first():
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    posted = {"old_tg": "2026-08-01T10:00:00+00:00", "new_vk": "1790000000", "mid_tg": "2026-09-01T10:00:00+00:00", "none": None}
    for i, (author, when) in enumerate(posted.items()):
        row = catcher_db.insert_raw_message(conn, source["id"], str(i), author, "ищу школу", None, when)
        catcher_db.add_candidate(conn, row, "ищу школу", "r", None, "o", "high")
    order = [c["author_name"] for c in catcher_db.list_candidates(conn)]
    assert order == ["new_vk", "mid_tg", "old_tg", "none"]


def test_offtopic_tail_is_marked_processed(monkeypatch):
    """Последняя пачка целиком офтоп (предфильтр оставил 0) — сообщения всё равно разобраны."""
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "a", "всем привет", None, None)
    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: ([], True))
    catcher_pipeline.process_source(conn, source["id"])
    assert catcher_db.unprocessed_messages_for_source(conn, source["id"]) == []


def test_catcher_backend_switch(monkeypatch):
    from config import CONFIG
    import claude_code
    import llm
    calls = []
    monkeypatch.setattr(claude_code, "call_text", lambda s, u, model="sonnet", **kw: calls.append(("cc", model)) or "[]")
    monkeypatch.setattr(llm, "call_text", lambda s, u, **kw: calls.append(("api", kw.get("model"))) or "[]")
    monkeypatch.setattr(CONFIG, "catcher_llm_backend", "claude_code")
    catcher_pipeline._llm("s", "u", cheap=True)
    catcher_pipeline._llm("s", "u")
    monkeypatch.setattr(CONFIG, "catcher_llm_backend", "api")
    catcher_pipeline._llm("s", "u")
    assert calls == [("cc", "haiku"), ("cc", "sonnet"), ("api", None)]


def test_claude_code_env_has_no_api_keys(monkeypatch):
    import claude_code
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    env = claude_code._env()
    assert "ANTHROPIC_API_KEY" not in env and "LLM_API_KEY" not in env


def test_tg_peer_is_remembered_and_reused():
    import catcher_db
    import catcher_tg
    from telethon.tl.types import InputPeerChannel

    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/Parents_Chat")
    assert catcher_tg._cached_peer(source) is None
    assert catcher_tg._username_from_url(source["url"]) == "parents_chat"
    assert catcher_tg._username_from_url("https://t.me/+abc") is None

    catcher_db.save_tg_peer(conn, source["id"], "channel", 2223334445, 987654321)
    peer = catcher_tg._cached_peer(catcher_db.get_source(conn, source["id"]))
    assert peer == InputPeerChannel(2223334445, 987654321)
