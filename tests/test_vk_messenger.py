"""Диалоги продаж в личке VK (vk_messenger.py): чистая логика и ядро продавца с фейковым браузером."""
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import catcher_db
import db
import main
import pipeline
import ready_bot
import runtime
import vk_browser
import vk_messenger
from config import CONFIG

SELF = 1126515552
PEER = 13750322


def _in(key, text, peer=PEER, **kw):
    return {"key": str(key), "stack": str(key), "author": "Лид", "author_href": f"/id{peer}", "text": text, **kw}


def _out(key, text, **kw):
    return {"key": str(key), "stack": str(key), "author": "Я", "author_href": f"/id{SELF}", "text": text,
            "cls": "ConvoStack ConvoStack--out", **kw}


# --- белый список и профили ------------------------------------------------------------------

def test_local_and_page_profile_id():
    assert vk_messenger.local_profile_id("id13750322") == 13750322
    assert vk_messenger.local_profile_id("elenell") is None
    html = ('..."method":"users.get","request":{"user_ids":"elenell","fields":"x"},"version":"5.289",'
            '"response":[{"id":555777,"first_name_nom":"Алёна","domain":"elenell","sex":1}]...')
    assert vk_messenger.parse_profile_id(html, "elenell") == 555777
    assert vk_messenger.parse_profile_id(html, "other") is None
    assert vk_messenger.parse_profile_id("", "id42") == 42


def test_whitelist_skips_groups_self_and_collects_unresolved():
    peers, unresolved = vk_messenger.build_whitelist(
        ["id13750322", "elenell", "club123", "known", f"id{SELF}"],
        {"known": 777}, [{"id": 5, "vk_id": 999, "vk_path": "lead999"}, {"id": 6, "vk_id": 2000000001, "vk_path": None}],
        self_id=SELF,
    )
    assert set(peers) == {13750322, 777, 999}
    assert peers[999]["lead_id"] == 5
    assert unresolved == ["elenell"]


def test_whitelist_from_db_contacted_vk_candidates_and_cache(tmp_path):
    path = str(tmp_path / "vk.db")
    with db.session(path) as conn:
        catcher_db.migrate(conn)
        src = catcher_db.add_source(conn, "vk", "https://vk.com/club1")
        tg = catcher_db.add_source(conn, "tg", "https://t.me/x")
        for i, (source, author) in enumerate([(src, "id111"), (src, "elenell"), (src, "notcontacted"), (tg, "tguser")]):
            mid = catcher_db.insert_raw_message(conn, source["id"], str(i), "a", "t", None, None, author_username=author)
            catcher_db.add_candidate(conn, mid, "q", "r", None, "o")
            if author != "notcontacted":
                conn.execute("UPDATE catch_candidates SET status = 'contacted' WHERE raw_message_id = ?", (mid,))
        peers, unresolved = vk_messenger.load_whitelist(conn, SELF)
        assert set(peers) == {111} and unresolved == ["elenell"]
        vk_messenger.save_peer(conn, "elenell", 222)
        peers, unresolved = vk_messenger.load_whitelist(conn, SELF)
        assert set(peers) == {111, 222} and unresolved == []


# --- разбор диалога ----------------------------------------------------------------------------

def test_diff_new_incoming_after_last_outgoing():
    items = [_out(1, "Добрый день! Видел Ваш вопрос"), _in(2, "Здравствуйте"), _in(3, "а сколько стоит?")]
    d = vk_messenger.diff_dialog(items, 0, PEER, "id13750322", SELF, set(), set())
    assert d.context == [("менеджер", "Добрый день! Видел Ваш вопрос")]  # первое сообщение оператора
    assert d.pending_text == "Здравствуйте\nа сколько стоит?"
    assert d.newest_cmid == 3 and d.newest_in_cmid == 3 and d.manual == 1


def test_diff_bot_messages_skipped_and_manual_reply_silences_bot():
    items = [_in(4, "вопрос"), _out(5, "ответ бота"), _in(6, "ещё вопрос"), _out(7, "менеджер ответил сам")]
    d = vk_messenger.diff_dialog(items, 3, PEER, None, SELF, {5}, set())
    assert d.context == [("лид", "вопрос"), ("лид", "ещё вопрос"), ("менеджер", "менеджер ответил сам")]
    assert d.pending_text is None and d.manual == 1 and d.newest_cmid == 7


def test_diff_only_after_cursor_and_bot_text_fallback():
    items = [_in(1, "старое"), _out(2, "ответ бота без cmid"), _in(3, "новое")]
    d = vk_messenger.diff_dialog(items, 1, PEER, None, SELF, set(), {"ответ бота без cmid"})
    assert d.context == [] and d.pending_text == "новое"


def test_diff_screen_name_author_and_unknown_author_stops():
    items = [{"key": "1", "stack": "1", "author_href": "/elenell", "text": "привет"}]
    assert vk_messenger.diff_dialog(items, 0, 222, "elenell", SELF, set(), set()).pending_text == "привет"
    unknown = [{"key": "1", "stack": "1", "author_href": "", "text": "кто это?"}]
    d = vk_messenger.diff_dialog(unknown, 0, 222, "elenell", SELF, set(), set())
    assert d.pending_text is None and d.newest_cmid == 0  # ничего не трогаем


def test_diff_media_and_stale():
    d = vk_messenger.diff_dialog([_in(1, "", media=True)], 0, PEER, None, SELF, set(), set())
    assert d.pending_media and d.pending_text is None
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    old = [_in(1, "давно спрашивала", day="5 сентября", time="10:00")]
    d = vk_messenger.diff_dialog(old, 0, PEER, None, SELF, set(), set(), now=now, max_age=timedelta(days=2))
    assert d.stale and d.pending_text is None and d.context == [("лид", "давно спрашивала")]


def test_direction_by_class_when_no_author():
    assert vk_messenger.message_direction({"cls": "ConvoStack ConvoStack--out"}, PEER, None, SELF) == "out"
    assert vk_messenger.message_direction({"cls": "ConvoStack ConvoStack--in"}, PEER, None, SELF) == "in"
    assert vk_messenger.message_direction({"cls": "ConvoStack"}, PEER, None, SELF) is None


# --- лимиты и признаки блокировки ---------------------------------------------------------------

def test_rate_limits():
    now = datetime.now(timezone.utc)
    recent = [now - timedelta(minutes=i) for i in range(5)]
    old = [now - timedelta(hours=2)] * 50
    assert vk_messenger.rate_allows(recent + old, [], now, 20, 6) is None
    assert "в час на аккаунт" in vk_messenger.rate_allows(recent * 4, [], now, 20, 6)
    assert "в одном диалоге" in vk_messenger.rate_allows(recent, recent + recent[:1], now, 20, 6)


def test_detect_block():
    assert vk_messenger.detect_block("https://vk.ru/im", "Сообщения", SELF) is None
    assert vk_messenger.detect_block("https://vk.ru/im", "", None)  # разлогинен
    assert vk_messenger.detect_block("https://id.vk.com/auth", "", SELF)
    assert "подозрительная" in vk_messenger.detect_block("https://vk.ru/im", "Замечена Подозрительная активность", SELF)


def test_profile_lock_gives_priority_to_dialogs():
    lock = vk_browser._ProfileLock()
    assert lock.acquire("ловец")
    order = []

    def crawl():
        lock.acquire("ловец-2")
        order.append("ловец-2")
        lock.release()

    def dialogs():
        lock.acquire("диалоги", priority=True)
        order.append("диалоги")
        time.sleep(0.05)
        lock.release()

    t_dialogs = threading.Thread(target=dialogs)
    t_dialogs.start()
    time.sleep(0.05)
    t_crawl = threading.Thread(target=crawl)
    t_crawl.start()
    time.sleep(0.05)
    lock.release()
    t_dialogs.join(2)
    t_crawl.join(2)
    assert order == ["диалоги", "ловец-2"]
    assert lock.acquire("x", timeout=0.01) and not lock.acquire("y", timeout=0.01)


# --- ядро продавца через фейковый браузер -------------------------------------------------------

class _FakeSession:
    def __init__(self, items):
        self.items = items
        self.self_id = SELF
        self.sent: list[tuple[int, str]] = []

    async def call(self, fn, *args):
        return fn(self, *args)

    def read_dialog(self, peer_id, after):
        return [it for it in self.items if int(it["key"]) > after]

    def send(self, peer_id, text):
        self.sent.append((peer_id, text))
        key = max(int(it["key"]) for it in self.items) + 1
        self.items.append(_out(key, text))
        return key


def _setup(tmp_path, monkeypatch, score=50):
    db_path = str(tmp_path / "vkm.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    monkeypatch.setattr(CONFIG, "vk_sales_delay_min", 0)
    monkeypatch.setattr(CONFIG, "vk_sales_delay_max", 0)
    monkeypatch.setattr(main.guard, "check", lambda text: SimpleNamespace(is_injection=False, source="llm", reasoning=""))
    monkeypatch.setattr(main.alerts, "llm_configured", lambda: True)
    monkeypatch.setattr(main.pipeline, "score_message", lambda ctx: pipeline.ScoreResult(
        score=score, band=pipeline.band_for_score(score), reasoning="", source="llm"))
    monkeypatch.setattr(main.pipeline, "generate_touch", lambda band, stage, ctx: "Понял Вас. В каком классе ребёнок?")
    notes = []

    async def _notify(text):
        notes.append(text)

    async def _ok():
        return None

    monkeypatch.setattr(main.alerts, "notify_operators", _notify)
    monkeypatch.setattr(main.alerts, "report_llm_ok", _ok)
    vk_messenger.STATUS.halted = None
    return db_path, notes


HOOKS = vk_messenger.Hooks(process=main.process_lead_text, note_media=main.note_unanswered_media)


def _run(session, peer=PEER, path="id13750322", name="Елена"):
    return asyncio.run(vk_messenger._handle_dialog(session, HOOKS, peer, {"path": path, "lead_id": None},
                                                   {"peer": str(peer), "name": name, "preview": "p"}))


def test_vk_dialog_goes_through_same_core_as_telegram(tmp_path, monkeypatch):
    db_path, _ = _setup(tmp_path, monkeypatch)
    session = _FakeSession([_out(1, "Добрый день! Видел Ваш вопрос про СО"), _in(2, "Да, а сколько стоит?")])
    assert _run(session) is True
    assert session.sent == [(PEER, "Понял Вас. В каком классе ребёнок?")]
    with db.session(db_path) as conn:
        lead = conn.execute("SELECT * FROM leads WHERE vk_id = ?", (PEER,)).fetchone()
        assert lead["platform"] == "vk" and lead["tg_id"] == -PEER and lead["name"] == "Елена"
        assert lead["dialog_context"].split("\n") == [
            "менеджер: Добрый день! Видел Ваш вопрос про СО", "лид: Да, а сколько стоит?",
            "бот: Понял Вас. В каком классе ребёнок?"]
        assert vk_messenger.get_dialog(conn, PEER)["last_cmid"] == 3
        assert conn.execute("SELECT cmid FROM vk_sent").fetchone()[0] == 3

    # повторный тик без новых сообщений — ничего не шлём; ответ бота не считается ручным
    assert _run(session) is False and len(session.sent) == 1
    # менеджер написал руками — в историю «менеджер», бот молчит
    session.items.append(_out(4, "Я сам подключусь, минуту"))
    assert _run(session) is False
    with db.session(db_path) as conn:
        assert "менеджер: Я сам подключусь, минуту" in db.get_lead(
            conn, conn.execute("SELECT id FROM leads").fetchone()[0])["dialog_context"]


def test_vk_hot_lead_contact_and_card_with_vk_link(tmp_path, monkeypatch):
    db_path, notes = _setup(tmp_path, monkeypatch, score=90)
    monkeypatch.setattr(pipeline, "extract_order", lambda text: SimpleNamespace(
        tariff_name="Аттестация", price=15000.0, department="приёмная_комиссия", needs_estimator=False,
        summary="Аттестация, 9 класс"))
    session = _FakeSession([_out(1, "Добрый день!"), _in(2, "Хотим поступить, давайте созвонимся")])
    _run(session, peer=222, path="elenell", name="Алёна")
    assert session.sent[-1] == (222, pipeline.CALL_REQUEST_TEXT)
    session.items.append(_in(len(session.items) + 1, "не хочу давать номер", peer=222))
    _run(session, peer=222, path="elenell", name="Алёна")
    assert session.sent[-1] == (222, pipeline.ALTERNATIVES_TEXT_VK)  # «здесь, в ВК», а не «в тг»
    session.items.append(_in(len(session.items) + 1, "пишите сюда, вечером", peer=222))
    _run(session, peer=222, path="elenell", name="Алёна")
    assert session.sent[-1] == (222, pipeline.CONTACT_THANKS_TEXT)
    with db.session(db_path) as conn:
        lead = conn.execute("SELECT * FROM leads WHERE vk_id = 222").fetchone()
        order = conn.execute("SELECT * FROM orders").fetchone()
    assert lead["manual_mode"] == 1 and order["contact"] == "пишите сюда, вечером"
    card = ready_bot.card_text(order, lead)
    assert "https://vk.com/elenell" in card and "Алёна" in card
    # в ручном режиме бот молчит, но историю пишет
    session.items.append(_in(len(session.items) + 1, "спасибо", peer=222))
    sent_before = len(session.sent)
    _run(session, peer=222, path="elenell", name="Алёна")
    assert len(session.sent) == sent_before


def test_vk_dry_run_does_not_send(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(CONFIG, "dry_run", True)
    session = _FakeSession([_out(1, "Добрый день!"), _in(2, "а сколько стоит?")])
    _run(session)
    assert session.sent == []


def test_vk_sticker_notifies_manager(tmp_path, monkeypatch):
    db_path, notes = _setup(tmp_path, monkeypatch)
    session = _FakeSession([_out(1, "Добрый день!"), _in(2, "", media=True)])
    _run(session)
    assert session.sent == [] and notes and "вложение" in notes[0]


def test_warmup_for_vk_lead_goes_to_outbox(tmp_path, monkeypatch):
    db_path, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(CONFIG, "vk_sales_enabled", True)
    with db.session(db_path) as conn:
        lead = db.get_or_create_vk_lead(conn, PEER, "id13750322", "Елена")
        vk_messenger.save_dialog(conn, PEER, lead["id"], 3, "p")
    assert asyncio.run(runtime.send_to_lead(object(), -PEER, None, "текст прогрева")) is None
    with db.session(db_path) as conn:
        items = vk_messenger.pending_outbox(conn)
    assert [(i["vk_id"], i["text"]) for i in items] == [(PEER, "текст прогрева")]
    monkeypatch.setattr(CONFIG, "vk_sales_enabled", False)
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.send_to_lead(object(), -PEER, None, "ещё"))


def test_rate_limit_blocks_reply(tmp_path, monkeypatch):
    db_path, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(CONFIG, "vk_sales_max_per_dialog_hour", 1)
    with db.session(db_path) as conn:
        vk_messenger.record_sent(conn, PEER, 9, "x")
        assert "в одном диалоге" in vk_messenger.check_rate(conn, PEER)
        assert vk_messenger.check_rate(conn, 555) is None


# --- рабочие часы и частота обхода VK ---------------------------------------------------------

def _msk(hour):
    return datetime(2026, 9, 30, hour, 15, tzinfo=timezone(timedelta(hours=3)))


def test_working_hours_day_interval():
    assert vk_messenger.in_working_hours(_msk(9), "9-22")
    assert vk_messenger.in_working_hours(_msk(21), "9-22")
    assert not vk_messenger.in_working_hours(_msk(22), "9-22")
    assert not vk_messenger.in_working_hours(_msk(3), "9-22")


def test_working_hours_overnight_and_disabled():
    assert vk_messenger.in_working_hours(_msk(23), "22-6")
    assert not vk_messenger.in_working_hours(_msk(12), "22-6")
    assert vk_messenger.in_working_hours(_msk(3), "")
    assert vk_messenger.in_working_hours(_msk(3), "круглосуточно")


def test_vk_crawl_not_more_often_than_min_hours(monkeypatch):
    import catcher_service

    monkeypatch.setattr(CONFIG, "vk_crawl_min_hours", 20)
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert catcher_service.vk_crawl_wait(None, now) is None
    assert catcher_service.vk_crawl_wait((now - timedelta(hours=21)).isoformat(), now) is None
    wait = catcher_service.vk_crawl_wait((now - timedelta(hours=2)).isoformat(), now)
    assert wait == timedelta(hours=18)
    monkeypatch.setattr(CONFIG, "vk_crawl_min_hours", 0)
    assert catcher_service.vk_crawl_wait((now - timedelta(hours=2)).isoformat(), now) is None
