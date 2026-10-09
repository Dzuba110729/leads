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


def test_run_platform_fetches_everything_before_analysis(tmp_path, monkeypatch):
    """Сначала выгрузка всех чатов платформы, потом разбор; чужая платформа не трогается."""
    import asyncio

    import catcher_service
    import db
    from config import CONFIG

    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "t.db"))
    with db.session() as conn:
        catcher_db.migrate(conn)
        a = catcher_db.add_source(conn, "tg", "https://t.me/a")["id"]
        b = catcher_db.add_source(conn, "tg", "https://t.me/b")["id"]
        catcher_db.add_source(conn, "vk", "https://vk.com/c")
    events = []

    async def fake_fetch(source, client=None):
        events.append(("fetch", source["id"]))
        return 1

    def fake_process(conn, source_id):
        events.append(("analyze", source_id))
        return 0

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_client(platform):
        yield None

    monkeypatch.setattr(catcher_service, "_fetch", fake_fetch)
    monkeypatch.setattr(catcher_service, "_tg_client", fake_client)
    monkeypatch.setattr(catcher_service.catcher_pipeline, "process_source", fake_process)
    results = asyncio.run(catcher_service.run_sources(None, ("tg",)))
    assert [r.source_id for r in results] == [a, b]
    # Чаты разбираются параллельно (CATCHER_PARALLEL_SOURCES), порядок разбора не фиксирован.
    assert events[:2] == [("fetch", a), ("fetch", b)]
    assert sorted(events[2:]) == [("analyze", a), ("analyze", b)]


def _vk_run(tmp_path, monkeypatch, fetch):
    import asyncio

    import catcher_service
    import db
    from config import CONFIG

    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(CONFIG, "vk_crawl_pause_min", 0)
    monkeypatch.setattr(CONFIG, "vk_crawl_pause_max", 0)
    with db.session() as conn:
        catcher_db.migrate(conn)
        ids = [catcher_db.add_source(conn, "vk", f"https://vk.com/g{i}")["id"] for i in range(3)]
    monkeypatch.setattr(catcher_service, "_fetch", fetch)
    monkeypatch.setattr(catcher_service.catcher_pipeline, "process_source", lambda conn, sid: 0)
    return ids, asyncio.run(catcher_service.run_sources(None, ("vk",)))


def test_vk_crawl_stops_after_access_lost(tmp_path, monkeypatch):
    """VK не пускает аккаунт — остальные источники не трогаем, иначе VK отвечает проверкой hash429."""
    import vk_browser

    calls = []

    async def fetch(source, client=None):
        calls.append(source["id"])
        raise RuntimeError(vk_browser.NOT_LOGGED_IN)

    ids, results = _vk_run(tmp_path, monkeypatch, fetch)
    assert calls == [ids[0]]
    assert results[0].error and not results[0].skipped
    assert all(r.skipped and "остановлен" in r.skipped for r in results[1:])


def test_vk_crawl_continues_after_ordinary_error(tmp_path, monkeypatch):
    calls = []

    async def fetch(source, client=None):
        calls.append(source["id"])
        raise RuntimeError("Page.goto: Timeout 30000ms exceeded.")

    ids, results = _vk_run(tmp_path, monkeypatch, fetch)
    assert calls == ids


def test_vk_crawl_skipped_while_sales_halted(tmp_path, monkeypatch):
    import vk_messenger

    calls = []

    async def fetch(source, client=None):
        calls.append(source["id"])
        return 0

    monkeypatch.setattr(vk_messenger.STATUS, "halted", "VK показал капчу")
    ids, results = _vk_run(tmp_path, monkeypatch, fetch)
    assert calls == []
    assert all("диалоги VK остановлены" in r.skipped for r in results)


def test_second_pass_reads_batch_in_reverse(monkeypatch):
    """Второй проход - не копия первого: порядок обратный, номер сопоставляется с ним."""
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    for i, text in enumerate(["всем привет", "ищу дистанционную школу", "как дела"], start=1):
        catcher_db.insert_raw_message(conn, source["id"], str(i), "a", text, None, None)
    orders = []

    def fake_p1(messages, reply_map=None):
        orders.append([m["text"] for m in messages])
        # Оба прохода называют «ищу дистанционную школу» по номеру в СВОЁМ порядке
        n = next(i for i, m in enumerate(messages, start=1) if "школу" in m["text"])
        return [{"n": n, "confidence": "high", "reason": "ищет школу"}], True

    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", fake_p1)
    monkeypatch.setattr(catcher_pipeline.CONFIG, "p1_passes", 2)
    assert catcher_pipeline.process_source(conn, source["id"]) == 1
    assert sorted(orders) == sorted([["всем привет", "ищу дистанционную школу", "как дела"],
                                     ["как дела", "ищу дистанционную школу", "всем привет"]])
    candidate = conn.execute("SELECT * FROM catch_candidates").fetchone()
    assert candidate["quote"] == "ищу дистанционную школу"  # цитата - само сообщение


def test_batch_stays_unprocessed_if_any_pass_failed(monkeypatch):
    """Находку упавшего прохода никто не заменит - пачку разбираем ещё раз, найденное не теряется."""
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "a", "ищу дистанционную школу", None, None)
    answers = iter([([{"n": 1, "confidence": "high"}], True), ([], False)])
    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", lambda m, r=None: next(answers))
    monkeypatch.setattr(catcher_pipeline.CONFIG, "p1_passes", 2)
    monkeypatch.setattr(catcher_pipeline.CONFIG, "catcher_llm_workers", 1)
    assert catcher_pipeline.process_source(conn, source["id"]) == 1
    assert len(catcher_db.unprocessed_messages_for_source(conn, source["id"])) == 1


def test_openers_written_separately_and_filled_later(monkeypatch):
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    catcher_db.insert_raw_message(conn, source["id"], "1", "a", "ищу дистанционную школу", None, None)
    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch", lambda m, r=None: ([{"n": 1, "confidence": "high"}], True))

    # Модель заготовок не ответила — кандидат всё равно есть, заготовка пустая
    monkeypatch.setattr(catcher_pipeline, "_llm", lambda *a, **kw: None)
    catcher_pipeline.process_source(conn, source["id"])
    assert conn.execute("SELECT opener_text FROM catch_candidates").fetchone()[0] == ""

    # Следующий обход (новых сообщений нет) дописывает заготовку
    monkeypatch.setattr(catcher_pipeline, "_llm", lambda *a, **kw: '[{"n": 1, "opener_text": "Добрый день!"}]')
    catcher_pipeline.process_source(conn, source["id"])
    assert conn.execute("SELECT opener_text FROM catch_candidates").fetchone()[0] == "Добрый день!"


def test_prefilter_uses_its_own_bigger_batches(monkeypatch):
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    for i in range(7):
        catcher_db.insert_raw_message(conn, source["id"], str(i), "a", f"сообщение {i}", None, None)
    sizes = []
    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (sizes.append(len(m)) or ([], True)))
    monkeypatch.setattr(catcher_pipeline.CONFIG, "p1_prefilter_batch_size", 5)
    monkeypatch.setattr(catcher_pipeline.CONFIG, "p1_batch_size", 2)
    catcher_pipeline.process_source(conn, source["id"])
    assert sorted(sizes) == [2, 5]
    assert catcher_db.unprocessed_messages_for_source(conn, source["id"]) == []


def test_three_passes_use_different_orders(monkeypatch):
    conn = _mem_conn()
    source = catcher_db.add_source(conn, "tg", "https://t.me/test_group")
    for i in range(6):
        catcher_db.insert_raw_message(conn, source["id"], str(i), "a", f"сообщение {i}", None, None)
    orders = []
    monkeypatch.setattr(catcher_pipeline, "prefilter", lambda m, r=None: (m, True))
    monkeypatch.setattr(catcher_pipeline, "run_p1_on_batch",
                        lambda m, r=None: (orders.append(tuple(x["id"] for x in m)) or ([], True)))
    monkeypatch.setattr(catcher_pipeline.CONFIG, "p1_passes", 3)
    catcher_pipeline.process_source(conn, source["id"])
    assert len(orders) == 3 and len(set(orders)) == 3
    assert all(sorted(o) == sorted(orders[0]) for o in orders)


def test_vk_access_problem_tells_challenge_from_logout():
    import vk_browser

    assert vk_browser.access_problem("https://vk.com/challenge.html?hash429=x") == vk_browser.VK_CHALLENGE
    assert vk_browser.access_problem("https://vk.com/login?u=2") == vk_browser.NOT_LOGGED_IN
    assert vk_browser.access_problem("https://id.vk.com/auth") == vk_browser.NOT_LOGGED_IN
    assert vk_browser.access_problem("https://vk.com/im/convo/2000000001") is None
    assert vk_browser.access_problem("https://vk.com/famedu_co") is None


def test_vk_crawl_stops_after_challenge(tmp_path, monkeypatch):
    import vk_browser

    calls = []

    async def fetch(source, client=None):
        calls.append(source["id"])
        raise RuntimeError(vk_browser.VK_CHALLENGE)

    ids, results = _vk_run(tmp_path, monkeypatch, fetch)
    assert calls == [ids[0]]


def test_vk_crawl_pauses_between_sources(tmp_path, monkeypatch):
    """Между VK-источниками — пауза (перед первым не ждём), чтобы VK не принял обход за бота."""
    import asyncio

    import catcher_service

    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        sleeps.append(seconds)
        await real_sleep(0)

    async def fetch(source, client=None):
        return 0

    monkeypatch.setattr(catcher_service.asyncio, "sleep", fake_sleep)
    ids, results = _vk_run(tmp_path, monkeypatch, fetch)
    assert len(sleeps) == len(ids) - 1
