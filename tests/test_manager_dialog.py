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


def _hot_lead_with_order(db_path, tg_id, username):
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=tg_id, username=username)
        order = db.create_order(conn, lead_id=lead["id"], tariff="Аттестация", price=15000,
                                department="приёмная_комиссия", needs_estimator=False, summary="Аттестация")
    return lead, order


def _patch_hot(monkeypatch, score: int):
    import guard
    import pipeline

    monkeypatch.setattr(guard, "check", lambda text: SimpleNamespace(is_injection=False, source="llm", reasoning=""))
    monkeypatch.setattr(pipeline, "score_message", lambda text: pipeline.ScoreResult(
        score=score, band=pipeline.band_for_score(score), reasoning="", source="llm", refusal=False))
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(main.alerts, "notify_operators", fake_notify)
    return sent


def test_escalation_asks_for_phone_and_time_not_payment_link(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    sent = _patch_hot(monkeypatch, 88)
    import pipeline
    monkeypatch.setattr(pipeline, "extract_order", lambda text: SimpleNamespace(
        tariff_name="Аттестация", price=15000.0, department="приёмная_комиссия", needs_estimator=False,
        summary="Тариф «Аттестация»"))
    chat = SimpleNamespace(id=710, username="p10", bot=False, is_self=False)
    event = _Event(chat, "Да, давайте созвонимся")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == [pipeline.CALL_REQUEST_TEXT]
    assert "t.me/" not in event.replies[0]
    assert sent and "заявка №" in sent[0]

    # Повторный «горячий» ответ без номера: вторую заявку не создаём, просто просим номер ещё раз.
    event2 = _Event(chat, "Да, давайте", msg_id=2)
    asyncio.run(main.handle_incoming(event2, source="dm"))
    assert event2.replies == [pipeline.CALL_REQUEST_AGAIN_TEXT]
    with db.session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_phone_reply_goes_to_manager_and_bot_goes_silent(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    sent = _patch_hot(monkeypatch, 88)
    import pipeline
    lead, order = _hot_lead_with_order(db_path, 711, "p11")
    chat = SimpleNamespace(id=711, username="p11", bot=False, is_self=False)
    event = _Event(chat, "+7 999 123-45-67, после 18:00 по Москве")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == [pipeline.CONTACT_THANKS_TEXT]
    assert sent and "+7 999 123-45-67" in sent[0]
    with db.session(db_path) as conn:
        saved = db.get_order(conn, order["id"])
        assert saved["contact"] == "+7 999 123-45-67, после 18:00 по Москве"
        assert db.list_orders_for_reminders(conn) == []  # ждёт звонка — не напоминаем
    assert _lead(db_path, 711)["manual_mode"] == 1


def test_contact_reply_without_phone_also_goes_to_manager(tmp_path, monkeypatch):
    db_path = _setup(tmp_path, monkeypatch)
    sent = _patch_hot(monkeypatch, 88)
    _hot_lead_with_order(db_path, 712, "p12")
    chat = SimpleNamespace(id=712, username="p12", bot=False, is_self=False)
    event = _Event(chat, "Лучше пишите сюда в телеграм, после 18")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert sent and "после 18" in sent[0]
    assert _lead(db_path, 712)["manual_mode"] == 1


def test_contact_detection():
    import pipeline
    for text in ("+7 999 123-45-67", "в ватсап, вечером", "звоните в любое время", "завтра в 15"):
        assert pipeline.looks_like_contact(text), text
    for text in ("а сколько стоит?", "Да", "интересно, расскажите", "а можно максимально подробно?"):
        assert not pipeline.looks_like_contact(text), text


def test_cashier_bot_is_off_by_default():
    assert CONFIG.cashier_bot_active is False


def test_phone_extraction():
    import pipeline
    assert pipeline.extract_phone("8 (999) 123-45-67 вечером") == "8 (999) 123-45-67"
    assert pipeline.extract_phone("+995 555 12 34 56") == "+995 555 12 34 56"
    assert pipeline.extract_phone("завтра в 15:00, 12.10.2026") is None


def test_phone_chosen_without_number_asks_for_number(tmp_path, monkeypatch):
    import pipeline
    db_path = _setup(tmp_path, monkeypatch)
    sent = _patch_hot(monkeypatch, 88)
    _hot_lead_with_order(db_path, 713, "p13")
    chat = SimpleNamespace(id=713, username="p13", bot=False, is_self=False)
    event = _Event(chat, "Лучше по телефону, вечером")
    asyncio.run(main.handle_incoming(event, source="dm"))
    assert event.replies == [pipeline.PHONE_NUMBER_REQUEST_TEXT] and sent == []
    event2 = _Event(chat, "8 999 123 45 67", msg_id=2)
    asyncio.run(main.handle_incoming(event2, source="dm"))
    assert event2.replies == [pipeline.CONTACT_THANKS_TEXT] and "8 999 123 45 67" in sent[0]


def test_contact_steps_phone_first():
    import pipeline
    asked_phone = f"бот: {pipeline.PHONE_NUMBER_REQUEST_TEXT}"
    offered = f"бот: {pipeline.ALTERNATIVES_TEXT}"
    assert pipeline.contact_step("8 999 123 45 67, вечером", "") == "done"
    assert pipeline.contact_step("В тг можно звонить.", "") == "done"
    assert pipeline.contact_step("вечером после 18", "") == "ask_phone"
    assert pipeline.contact_step("после 19", asked_phone) == "offer_alternatives"
    assert pipeline.contact_step("не хочу давать номер", "") == "offer_alternatives"
    assert pipeline.contact_step("лучше по почте", offered) == "ask_email"
    assert pipeline.contact_step("mama@mail.ru", offered) == "done"
    assert pipeline.contact_step("а сколько стоит?", "") is None


def test_touch_prompt_uses_manager_style(monkeypatch):
    import llm
    import pipeline
    seen = {}
    monkeypatch.setattr(llm, "call_text", lambda system, text, **kw: seen.setdefault("system", system) and "ок")
    pipeline.generate_touch("warm", "интерес", "лид: сколько стоит?")
    assert "СТИЛЬ ПЕРЕПИСКИ" in seen["system"] and "Понял Вас" in seen["system"]


def test_ready_card_contains_contact_dialog_and_button(tmp_path, monkeypatch):
    import ready_bot
    db_path = _setup(tmp_path, monkeypatch)
    lead, order = _hot_lead_with_order(db_path, 714, "p14")
    with db.session(db_path) as conn:
        db.append_dialog_context(conn, lead["id"], "лид", "Хочу только аттестацию <9 класс>")
        db.set_order_contact(conn, order["id"], "В тг можно звонить")
        order, lead = db.get_order(conn, order["id"]), db.get_lead(conn, lead["id"])
    with db.session(db_path) as conn:
        db.set_lead_name(conn, lead["id"], db.display_name(SimpleNamespace(first_name="Анна", last_name="Смирнова")))
        lead = db.get_lead(conn, lead["id"])
    text = ready_bot.card_text(order, lead)
    assert "Анна Смирнова (@p14)" in text and "@p14" in text and "В тг можно звонить" in text and "&lt;9 класс&gt;" in text
    kb = ready_bot.card_keyboard(order)
    assert kb.inline_keyboard[0][0].callback_data == f"ready:done:{order['id']}"
    with db.session(db_path) as conn:
        assert db.ready_orders_not_passed(conn)[0]["id"] == order["id"]
        assert db.mark_passed_to_specialist(conn, order["id"], "@me") is True
        assert db.mark_passed_to_specialist(conn, order["id"], "@other") is False  # вторая копия
        order = db.get_order(conn, order["id"])
        assert db.ready_orders_not_passed(conn) == []
    assert ready_bot.card_keyboard(order) is None
    assert "Передан специалисту" in ready_bot.card_text(order, lead)


def test_ready_card_is_sent_to_feed_when_bot_configured(tmp_path, monkeypatch):
    import ready_bot
    db_path = _setup(tmp_path, monkeypatch)
    sent = _patch_hot(monkeypatch, 88)
    _hot_lead_with_order(db_path, 715, "p15")
    got = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            got.append((chat_id, text))
            return SimpleNamespace(message_id=len(got))

    monkeypatch.setattr(runtime, "ready_bot", FakeBot())
    monkeypatch.setattr(CONFIG, "ready_leads_chat_ids", [1, 2])
    chat = SimpleNamespace(id=715, username="p15", bot=False, is_self=False)
    asyncio.run(main.handle_incoming(_Event(chat, "звоните в тг вечером"), source="dm"))
    assert [c for c, _ in got] == [1, 2] and "звоните в тг вечером" in got[0][1]
    assert sent == []  # дошло в ленту — дублировать в ТГ-агента не нужно
    with db.session(db_path) as conn:
        assert conn.execute("SELECT ready_msgs FROM orders").fetchone()[0] == "1:1,2:2"


def test_megabitra_payload_and_phone_format(tmp_path, monkeypatch):
    import megabitra
    db_path = _setup(tmp_path, monkeypatch)
    for k, v in {"megabitra_api_key": "k", "megabitra_offer": "3", "megabitra_flow": "100",
                 "megabitra_lead_ip": "1.2.3.4"}.items():
        monkeypatch.setattr(CONFIG, k, v)
    assert megabitra.enabled()
    assert megabitra.normalize_phone("8 (999) 123-45-67") == "79991234567"
    assert megabitra.normalize_phone("+381 64 123 4567") == "381641234567"
    assert megabitra.normalize_phone("в тг") is None
    lead, order = _hot_lead_with_order(db_path, 716, "p16")
    with db.session(db_path) as conn:
        db.set_lead_name(conn, lead["id"], "Ольга")
        db.set_order_contact(conn, order["id"], "+7 999 123-45-67, после 18, olga@mail.ru")
        order, lead = db.get_order(conn, order["id"]), db.get_lead(conn, lead["id"])
    p = megabitra.build_payload(order, lead, "1.2.3.4")
    assert p["phone"] == "79991234567" and p["name"] == "Ольга" and p["email"] == "olga@mail.ru"
    assert p["flow"] == "100" and p["offer"] == "3" and p["ip"] == "1.2.3.4" and p["sub1"] == str(order["id"])
    assert "@p16" in p["comment"] and "после 18" in p["comment"]
    with db.session(db_path) as conn:
        db.set_order_contact(conn, order["id"], "лучше в тг")
        no_phone = db.get_order(conn, order["id"])
    monkeypatch.setattr(CONFIG, "megabitra_no_phone", "")
    assert megabitra.build_payload(no_phone, lead, "1.2.3.4") is None
    monkeypatch.setattr(CONFIG, "megabitra_no_phone", "79999999999")
    p = megabitra.build_payload(no_phone, lead, "1.2.3.4")
    assert p["phone"] == "79999999999" and "лучше в тг" in p["comment"]
    assert megabitra.describe({"status": "ok", "id": 55}) == "лид #55 принят"
    assert "уже есть" in megabitra.describe({"status": "error", "error": "duplicate"})


def test_megabitra_is_off_until_configured():
    import megabitra
    assert CONFIG.megabitra_offer == "" or megabitra.enabled() is bool(CONFIG.megabitra_flow and CONFIG.megabitra_lead_ip)
