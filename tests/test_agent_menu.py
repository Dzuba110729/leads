import asyncio
import json

import agent_bot
import agent_format
import agent_menu
import catcher_db
import config
import db
from config import CONFIG


def _db(tmp_path, monkeypatch):
    path = str(tmp_path / "m.db")
    monkeypatch.setattr(CONFIG, "db_path", path)
    return path


def _lead_with_order(conn, tg_id=100, username="ivan", score=85):
    lead = db.get_or_create_lead(conn, tg_id, username)
    db.append_dialog_context(conn, lead["id"], "лид", "хотим перейти на семейное <срочно>")
    db.append_dialog_context(conn, lead["id"], "бот", "Расскажу про тарифы")
    db.set_lead_score(conn, lead["id"], score)
    order = db.create_order(conn, lead["id"], "Экстернат", 50000, "приёмная", False, "хочет с 1 октября")
    return lead, order


# --- форматирование ---------------------------------------------------------------------

def test_md_to_html_escapes_and_converts():
    out = agent_format.md_to_html("## Итог\n**Горячих: 2** <b>x</b> & `код`\n- пункт\n[сайт](https://og1.ru/a?b=1&c=2)")
    assert "<b>Итог</b>" in out
    assert "<b>Горячих: 2</b>" in out
    assert "&lt;b&gt;x&lt;/b&gt; &amp;" in out
    assert "<code>код</code>" in out
    assert "• пункт" in out
    assert '<a href="https://og1.ru/a?b=1&amp;c=2">сайт</a>' in out


def test_split_text_respects_limit_and_keeps_lines():
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(200))
    chunks = agent_format.split_text(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


# --- память ---------------------------------------------------------------------------------

def test_history_starts_with_user_and_alternates(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        for role, text in [("assistant", "старый хвост"), ("user", "a"), ("user", "b"), ("assistant", "c")]:
            db.add_agent_message(conn, 1, role, text)
        history = db.load_agent_history(conn, 1, 10)
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[0]["content"] == "a\n\nb"
        db.clear_agent_history(conn, 1)
        assert db.load_agent_history(conn, 1, 10) == []


def test_failed_answer_leaves_no_history(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    class Boom:
        class messages:
            @staticmethod
            async def create(**kw):
                raise RuntimeError("сеть")

    monkeypatch.setattr(agent_bot, "_client", Boom())
    try:
        asyncio.run(agent_bot.answer(None, 5, "привет"))
    except RuntimeError:
        pass
    with db.session() as conn:
        assert db.load_agent_history(conn, 5, 10) == []


# --- инструменты модели ---------------------------------------------------------------------

def test_lead_and_order_tools(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        lead, order = _lead_with_order(conn)
        db.get_or_create_lead(conn, 200, None)
    hot = json.loads(agent_bot._tool_list_leads("hot", 5))
    assert [l["who"] for l in hot] == ["@ivan"]
    card = json.loads(agent_bot._tool_get_lead("@Ivan"))
    assert card["score"] == 85 and card["order"]["status"] == "Заявка"
    assert card["dialog_last_messages"][-1] == "бот: Расскажу про тарифы"
    assert "error" in json.loads(agent_bot._tool_get_lead("@nobody"))
    orders = json.loads(agent_bot._tool_list_orders("заявка", 10))
    assert orders[0]["id"] == order["id"]


# --- экраны меню ------------------------------------------------------------------------------

def test_summary_counts_today(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        _lead_with_order(conn)
    d = agent_menu.summary_data()
    assert d["new_leads"] == 1 and d["hot_today"] == 1 and d["new_orders"] == 1
    assert d["open_orders"] == {"заявка": 1}
    text, _ = agent_menu.render_summary()
    assert "Горячих (80+): 1" in text


def test_lead_card_escapes_dialog(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        lead, order = _lead_with_order(conn)
    text, kb = agent_menu.render_lead_card(lead["id"])
    assert "&lt;срочно&gt;" in text and "🔥 85" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"ord:{order['id']}" in datas
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/ivan"]


def test_order_card_and_confirm(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        _, order = _lead_with_order(conn)
    text, kb = agent_menu.render_order_card(order["id"])
    assert "Пробный день" in kb.inline_keyboard[0][0].text
    text, kb = agent_menu.render_order_confirm(order["id"])
    assert "«Заявка» в «Пробный день»" in text
    assert kb.inline_keyboard[0][0].callback_data == f"ord:advok:{order['id']}"


def test_candidate_paging(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        catcher_db.migrate(conn)
        src = catcher_db.add_source(conn, "tg", "https://t.me/parents")
        for i in range(2):
            mid = catcher_db.insert_raw_message(
                conn, source_chat_id=src["id"], external_id=str(i), author="Оля", text="ищем школу",
                url=f"https://t.me/parents/{i}", posted_at="2026-09-20T10:00:00+00:00",
                author_username="olya", author_tg_id=7,
            )
            catcher_db.add_candidate(conn, raw_message_id=mid, quote=f"ищем школу {i}", reason="ищет",
                                     contact_url=None, opener_text="Здравствуйте")
    text, kb = agent_menu.render_candidate(0)
    assert "Кандидат 1 из 2" in text
    done = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data and b.callback_data.startswith("cat:done:")][0]
    with db.session() as conn:
        catcher_db.mark_contacted(conn, int(done.split(":")[2]))
    text, _ = agent_menu.render_candidate(0)
    assert "Кандидат 1 из 1" in text
    with db.session() as conn:
        conn.execute("UPDATE catch_candidates SET status = 'contacted'")
    text, _ = agent_menu.render_candidate(3)
    assert "Новых кандидатов нет" in text


# --- переключатели ----------------------------------------------------------------------------

def test_toggle_updates_config_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=1\nDRY_RUN=0\n# коммент\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    monkeypatch.setattr(CONFIG, "scheduler_enabled", False)
    agent_menu.apply_toggle("dry", "on")
    agent_menu.apply_toggle("sch", "on")
    assert CONFIG.dry_run is True and CONFIG.scheduler_enabled is True
    assert env.read_text(encoding="utf-8") == "A=1\nDRY_RUN=1\n# коммент\nSCHEDULER_ENABLED=1\n"


def test_system_view_buttons_match_state(monkeypatch):
    monkeypatch.setattr(CONFIG, "dry_run", False)
    monkeypatch.setattr(CONFIG, "scheduler_enabled", False)
    text, kb = agent_menu.render_system()
    assert "Боевой режим: ВКЛ" in text
    assert kb.inline_keyboard[0][0].callback_data == "sys:dry:on"
    assert kb.inline_keyboard[1][0].callback_data == "sys:sch:on"


def test_parse_source_link():
    assert agent_menu.parse_source_link("https://t.me/parents_chat/") == ("tg", "https://t.me/parents_chat")
    assert agent_menu.parse_source_link("@parents_chat") == ("tg", "https://t.me/parents_chat")
    assert agent_menu.parse_source_link("t.me/parents") == ("tg", "https://t.me/parents")
    assert agent_menu.parse_source_link("https://vk.com/school_club") == ("vk", "https://vk.com/school_club")
    assert agent_menu.parse_source_link("привет") is None
    assert agent_menu.parse_source_link("https://t.me/") is None


def test_add_source_rejects_duplicates(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert agent_menu.add_source_from_text("https://t.me/parents")[0] is True
    ok, note = agent_menu.add_source_from_text("@parents")
    assert ok is False and "Уже были подключены" in note
    text, kb = agent_menu.render_sources(pick=False)
    assert "https://t.me/parents" in text
    assert kb.inline_keyboard[0][0].callback_data.startswith("cat:toggle:")


def test_rundoc_button_starts_job_with_export(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    text, kb = agent_menu.render_catcher_menu()
    assert kb.inline_keyboard[0][0].callback_data == "cat:rundoc"
    calls = []
    monkeypatch.setattr(agent_bot, "start_catcher_job", lambda bot, chat, ids, export: calls.append((ids, export)) or {"started": True})
    monkeypatch.setattr(CONFIG, "agent_allowed_ids", [1])

    class Msg:
        chat = type("C", (), {"id": 1})()
        async def edit_text(self, *a, **k): pass

    class Cb:
        data = "cat:rundoc"
        from_user = type("U", (), {"id": 1})()
        message = Msg()
        async def answer(self, *a, **k): pass

    asyncio.run(agent_menu.cb_cat_run(Cb(), bot=None))
    assert calls == [([], True)]


def test_parse_normalizes_message_links_and_rejects_invites():
    assert agent_menu.parse_source_link("https://t.me/parents/1234") == ("tg", "https://t.me/parents")
    assert agent_menu.parse_source_link("https://t.me/+AbCdEf") is None
    assert agent_menu.parse_source_link("https://t.me/joinchat/AbCd") is None
    assert agent_menu.parse_source_link("https://vk.com/club1?w=wall-1_2") == ("vk", "https://vk.com/club1")


def test_add_list_of_sources(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    agent_menu.add_sources_from_text("https://t.me/old")
    text = """Чаты родителей:
1. https://t.me/parents_msk
2. @parents_spb, https://vk.com/homeschool
3. https://t.me/old
https://t.me/parents_msk/555
https://t.me/+secret"""
    r = agent_menu.add_sources_from_text(text)
    assert r["added"] == ["https://t.me/parents_msk", "https://t.me/parents_spb", "https://vk.com/homeschool"]
    assert r["duplicates"] == ["https://t.me/old", "https://t.me/parents_msk"]
    assert r["invalid"] == ["https://t.me/+secret"]
    assert "Добавлено чатов: 3" in agent_menu.describe_added(r)


def test_only_high_candidates(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with db.session() as conn:
        catcher_db.migrate(conn)
        src = catcher_db.add_source(conn, "tg", "https://t.me/parents")
        for i, conf in enumerate(["maybe", "high", "maybe"]):
            mid = catcher_db.insert_raw_message(
                conn, source_chat_id=src["id"], external_id=str(i), author="Оля", text="школа",
                url=f"https://t.me/parents/{i}", posted_at="2026-09-20T10:00:00+00:00",
                author_username=f"u{i}", author_tg_id=i,
            )
            catcher_db.add_candidate(conn, raw_message_id=mid, quote=f"цитата {i}", reason="r",
                                     contact_url=None, opener_text="Здравствуйте", confidence=conf)
    _, kb = agent_menu.render_catcher_menu()
    assert [b.text for b in kb.inline_keyboard[2]] == ["🎯 Только точные (1)", "🆕 Все (3)"]
    text, kb = agent_menu.render_candidate(0, only_high=True)
    assert "Точный кандидат 1 из 1" in text and "цитата 1" in text
    done = [b.callback_data for row in kb.inline_keyboard for b in row if (b.callback_data or "").startswith("cat:done:")][0]
    assert done.endswith(":h")
    with db.session() as conn:
        catcher_db.mark_contacted(conn, int(done.split(":")[2]))
    text, kb = agent_menu.render_candidate(0, only_high=True)
    assert "Точных кандидатов не осталось" in text and "Под вопросом ещё 2" in text


def test_llm_error_is_explained():
    import llm
    llm.last_error = "Error code: 400 - Your credit balance is too low to access the Anthropic API."
    assert "закончились деньги" in llm.describe_last_error()
    llm.last_error = None
