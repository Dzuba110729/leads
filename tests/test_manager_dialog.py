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


# --- отказ, вложения, догонялка ------------------------------------------------------------

import pipeline


class _MediaEvent(_Event):
    def __init__(self, chat, msg_id=1, kind="sticker"):
        super().__init__(chat, "", msg_id)
        self.media = object()
        for k in ("voice", "video_note", "sticker", "gif", "photo", "video", "document"):
            setattr(self, k, k == kind)
        self.file = SimpleNamespace(duration=5)


def _patch_llm(monkeypatch, score=0, refusal=False, closing="Рад, что всё сложилось! Если что-то изменится — пишите."):
    monkeypatch.setattr(main.guard, "check", lambda text: SimpleNamespace(is_injection=False, source="llm", reasoning=""))
    monkeypatch.setattr(main.alerts, "llm_configured", lambda: True)
    monkeypatch.setattr(main.pipeline, "score_message", lambda ctx: pipeline.ScoreResult(
        score=score, band=pipeline.band_for_score(score), reasoning="", source="llm", refusal=refusal))
    monkeypatch.setattr(main.pipeline, "generate_closing", lambda ctx: closing)
    monkeypatch.setattr(main.pipeline, "generate_touch", lambda band, stage, ctx: "касание")
    notes = []

    async def _notify(text):
        notes.append(text)

    async def _ok():
        return None

    monkeypatch.setattr(main.alerts, "notify_operators", _notify)
    monkeypatch.setattr(main.alerts, "report_llm_ok", _ok)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    return notes


def test_refusal_gets_one_polite_closing_and_stops_warmup(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    _patch_llm(monkeypatch, score=0, refusal=True)
    chat = SimpleNamespace(id=710, username="el", bot=False, is_self=False)
    first = _Event(chat, "здравствуйте, нас взяли в школу, спасибо", msg_id=1)
    asyncio.run(main.handle_incoming(first, source="dm"))
    assert first.replies == ["Рад, что всё сложилось! Если что-то изменится — пишите."]
    lead = _lead(db_path, 710)
    assert lead["declined_at"] and lead["next_step_idx"] == 3

    again = _Event(chat, "спасибо", msg_id=2)
    asyncio.run(main.handle_incoming(again, source="dm"))
    assert again.replies == []  # второй раз не прощаемся


def test_cold_without_refusal_stays_silent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_llm(monkeypatch, score=0, refusal=False)
    event = _Event(SimpleNamespace(id=711, username="x", bot=False, is_self=False), "ок")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == []


def test_same_message_is_not_handled_twice(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_llm(monkeypatch, score=50)
    chat = SimpleNamespace(id=712, username="y", bot=False, is_self=False)
    event = _Event(chat, "а сколько стоит?", msg_id=7)
    asyncio.run(main.handle_incoming(event, source="dm"))
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == ["касание"]


def test_sticker_notifies_manager_and_bot_is_silent(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    notes = _patch_llm(monkeypatch)
    event = _MediaEvent(SimpleNamespace(id=713, username="likia", bot=False, is_self=False), kind="sticker")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == []
    assert len(notes) == 1 and "@likia" in notes[0] and "стикер" in notes[0]
    assert _lead(db_path, 713)["dialog_context"] == "лид: [стикер]"


def test_voice_is_transcribed_and_answered(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    notes = _patch_llm(monkeypatch, score=50)

    async def _fake_transcribe(event):
        return "а сколько стоит обучение"

    monkeypatch.setattr(main, "_transcribe", _fake_transcribe)
    event = _MediaEvent(SimpleNamespace(id=714, username="v", bot=False, is_self=False), kind="voice")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == ["касание"] and notes == []
    assert "лид: [голосовое] а сколько стоит обучение" in _lead(db_path, 714)["dialog_context"]


class _Msg(_Event):
    def __init__(self, chat, text, msg_id, date, out):
        super().__init__(chat, text, msg_id)
        self.date, self.out, self.media, self.action = date, out, None, None


class _FakeClient:
    def __init__(self, dialogs):
        self._dialogs = dialogs  # [(dialog, [messages newest first])]

    async def iter_dialogs(self, limit=100):
        for d, _ in self._dialogs:
            yield d

    async def iter_messages(self, entity, limit=30):
        for d, msgs in self._dialogs:
            if d.entity is entity:
                for m in msgs:
                    yield m


def test_catch_up_answers_missed_and_skips_already_known(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    db_path = _setup(tmp_path, monkeypatch)
    _patch_llm(monkeypatch, score=50)
    now = datetime.now(timezone.utc)
    alive = (now - timedelta(minutes=5)).isoformat()
    chat = SimpleNamespace(id=720, username="gap", bot=False, is_self=False)
    with db.session(db_path) as conn:
        lead = db.ensure_lead(conn, 720, "gap")
        db.append_dialog_context(conn, lead["id"], "бот", "старый ответ бота")
    old = _Msg(chat, "давнее", 1, now - timedelta(days=1), out=False)
    bot_reply = _Msg(chat, "старый ответ бота", 2, now - timedelta(minutes=4), out=True)
    manager = _Msg(chat, "менеджер написал в паузе", 3, now - timedelta(minutes=3), out=True)
    lead_msg = _Msg(chat, "а сколько стоит?", 4, now - timedelta(minutes=2), out=False)
    dialog = SimpleNamespace(is_user=True, date=lead_msg.date, entity=chat)
    client = _FakeClient([(dialog, [lead_msg, manager, bot_reply, old])])

    asyncio.run(main.catch_up_missed(client, alive))
    assert lead_msg.replies == ["касание"] and old.replies == []
    ctx = _lead(db_path, 720)["dialog_context"]
    assert ctx.count("старый ответ бота") == 1
    assert "менеджер: менеджер написал в паузе" in ctx and "лид: а сколько стоит?" in ctx

    asyncio.run(main.catch_up_missed(client, alive))  # повторный прогон — без дублей
    assert lead_msg.replies == ["касание"]


def test_catch_up_skipped_without_previous_heartbeat():
    assert asyncio.run(main.catch_up_missed(_FakeClient([]), None)) == 0


def test_send_to_lead_falls_back_to_username():
    class _Client:
        def __init__(self):
            self.sent = []

        async def send_message(self, peer, text):
            if isinstance(peer, int):
                raise ValueError("Could not find the input entity")
            self.sent.append(peer)
            return SimpleNamespace(id=5)

    client = _Client()
    asyncio.run(runtime.send_to_lead(client, 730, "lead730", "текст"))
    assert client.sent == ["lead730"] and runtime.is_auto_sent(730, 5)
