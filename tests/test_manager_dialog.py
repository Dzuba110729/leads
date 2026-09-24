import asyncio
from types import SimpleNamespace

import agent_menu
import db
import main
import runtime
from config import CONFIG


class _Event:
    def __init__(self, chat, text, msg_id=1, outgoing=False):
        self.chat_id = chat.id
        self.id = msg_id
        self.raw_text = text
        self._chat = chat
        self.replies: list[str] = []

    async def get_chat(self):
        return self._chat

    async def get_sender(self):
        return self._chat

    async def reply(self, text):
        self.replies.append(text)
        return SimpleNamespace(id=999)


def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "m.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    monkeypatch.setattr(CONFIG, "operator_chat", "555")
    monkeypatch.setattr(main, "AUTO_SENT_SETTLE_SECONDS", 0)
    return db_path


def _lead(db_path, tg_id):
    with db.session(db_path) as conn:
        return conn.execute("SELECT * FROM leads WHERE tg_id = ?", (tg_id,)).fetchone()


def test_manager_message_goes_into_dialog(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    chat = SimpleNamespace(id=701, username="parent", bot=False, is_self=False)
    asyncio.run(main.handle_outgoing(_Event(chat, "Здравствуйте! Видела ваш вопрос про СО")))
    lead = _lead(db_path, 701)
    assert lead["dialog_context"] == "менеджер: Здравствуйте! Видела ваш вопрос про СО"


def test_bot_own_reply_is_not_recorded_as_manager(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    chat = SimpleNamespace(id=702, username="p2", bot=False, is_self=False)
    runtime.remember_auto_sent(702, SimpleNamespace(id=42))
    asyncio.run(main.handle_outgoing(_Event(chat, "ответ ИИ", msg_id=42)))
    assert _lead(db_path, 702) is None


def test_outgoing_to_operator_bot_or_self_is_ignored(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    for chat in (
        SimpleNamespace(id=555, username=None, bot=False, is_self=False),
        SimpleNamespace(id=703, username="somebot", bot=True, is_self=False),
        SimpleNamespace(id=704, username="me", bot=False, is_self=True),
    ):
        asyncio.run(main.handle_outgoing(_Event(chat, "текст")))
        assert _lead(db_path, chat.id) is None


def test_manual_mode_bot_stays_silent_but_keeps_context(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=705, username="p5")
        db.set_manual_mode(conn, lead["id"], True)
    chat = SimpleNamespace(id=705, username="p5", bot=False, is_self=False)
    event = _Event(chat, "Хочу записаться, куда платить?")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == []
    assert _lead(db_path, 705)["dialog_context"] == "лид: Хочу записаться, куда платить?"


def test_manual_mode_excluded_from_warmup(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=706, username="p6")
        db.set_manual_mode(conn, lead["id"], True)
        assert [l["tg_id"] for l in db.active_leads_for_warmup(conn)] == []


def test_lead_card_toggle_button(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=707, username="p7")
    _, kb = agent_menu.render_lead_card(lead["id"])
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"lead_manual:{lead['id']}:on" in datas
    with db.session(db_path) as conn:
        db.set_manual_mode(conn, lead["id"], True)
    text, kb = agent_menu.render_lead_card(lead["id"])
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"lead_manual:{lead['id']}:off" in datas
    assert "Диалог ведёте вы" in text


def test_fmt_posted_handles_tg_and_vk_dates():
    assert "(" in agent_menu._fmt_posted("2026-09-20T10:00:00+00:00")
    assert agent_menu._fmt_posted("1758362400").startswith("20.09.2025")
    assert agent_menu._fmt_posted(None) == "дата неизвестна"
