import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import contact_reminders as cr
import db
import main
import pipeline
from config import CONFIG

MSK = cr.MSK


def _msk(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK).astimezone(timezone.utc)


def test_ladder_timing():
    asked = _msk(16, 11)
    assert cr.due_stage(asked, 0, None, _msk(16, 14)) is None  # 3 часа — рано
    assert cr.due_stage(asked, 0, None, _msk(16, 14, 40)) == 1
    assert cr.due_stage(asked, 1, _msk(16, 14, 40), _msk(16, 20)) is None  # второе — только завтра
    assert cr.due_stage(asked, 1, _msk(16, 14, 40), _msk(17, 9, 30)) is None  # до 10 утра
    assert cr.due_stage(asked, 1, _msk(16, 14, 40), _msk(17, 10, 5)) == 2
    assert cr.due_stage(asked, 2, _msk(17, 10, 5), _msk(18, 12)) is None  # 3 дня не прошло
    assert cr.due_stage(asked, 2, _msk(17, 10, 5), _msk(19, 11, 30)) == 3
    assert cr.due_stage(asked, 3, _msk(19, 11, 30), _msk(21, 12)) is None  # после третьего молчим


def test_no_reminders_at_night_or_for_old_orders():
    late = _msk(16, 20)  # просили в 20:00 — первое пришлось бы на ночь
    assert cr.due_stage(late, 0, None, _msk(16, 23, 50)) is None
    assert cr.due_stage(late, 0, None, _msk(17, 8)) is None
    assert cr.due_stage(late, 0, None, _msk(17, 10)) == 2  # утром сразу второе, без ночного первого
    assert cr.due_stage(_msk(1, 11), 0, None, _msk(16, 12)) is None  # заявке 2 недели


def test_text_uses_single_word_name_and_platform():
    lead = {"real_name": None, "name": "Илдар", "tg_id": 1, "vk_id": None, "platform": "tg"}
    assert cr.reminder_text(1, lead).startswith("Илдар, подскажите")
    assert "Добрый день, Илдар!" in cr.reminder_text(2, lead) and "в тг" in cr.reminder_text(2, lead)
    nameless = dict(lead, name="🍀")
    assert cr.reminder_text(1, nameless).startswith("Подскажите")


def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "r.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    sent = []

    class _Channel:
        def __init__(self, lead):
            self.chat_id = lead["tg_id"]

        async def reply(self, text):
            sent.append((self.chat_id, text))
            return SimpleNamespace(id=1)

    monkeypatch.setattr(main, "LeadChannel", _Channel)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    return db_path, sent


def _ready_lead(db_path, tg_id, last_role="бот", manual=False, contact=None):
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=tg_id, username=f"u{tg_id}")
        db.set_lead_name(conn, lead["id"], "Илдар")
        db.append_dialog_context(conn, lead["id"], "лид", "Можно завтра")
        db.append_dialog_context(conn, lead["id"], last_role, pipeline.CALL_REQUEST_TEXT)
        order = db.create_order(conn, lead_id=lead["id"], tariff="?", price=None, department="приёмная_комиссия",
                                needs_estimator=False, summary="11 класс")
        asked = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (asked, order["id"]))
        conn.execute("UPDATE leads SET last_seen_at = ? WHERE id = ?", (asked, lead["id"]))
        if manual:
            db.set_manual_mode(conn, lead["id"], True)
        if contact:
            db.set_order_contact(conn, order["id"], contact)
        return order["id"]


def test_only_silent_ready_leads_get_reminded(tmp_path, monkeypatch):
    db_path, sent = _setup(tmp_path, monkeypatch)
    waiting = _ready_lead(db_path, 1)
    _ready_lead(db_path, 2, manual=True)  # «Веду сам»
    _ready_lead(db_path, 3, contact="+7 999 123 45 67")  # контакт уже дал
    _ready_lead(db_path, 4, last_role="менеджер")  # менеджер уже пишет сам
    with db.session(db_path) as conn:  # обычный лид без заявки
        db.get_or_create_lead(conn, tg_id=5, username="u5")
    now = datetime.now(timezone.utc)
    if not (9 <= now.astimezone(MSK).hour < 22):
        now = now.replace(hour=9, tzinfo=timezone.utc)  # 12:00 МСК; лид писал за 4 часа до «сейчас»
        with db.session(db_path) as conn:
            asked = (now - timedelta(hours=4)).isoformat()
            conn.execute("UPDATE orders SET created_at = ?", (asked,))
            conn.execute("UPDATE leads SET last_seen_at = ?", (asked,))
    ids = asyncio.run(cr.run_once(now))
    assert ids == [waiting] and [tg for tg, _ in sent] == [1]
    assert sent[0][1].startswith("Илдар, подскажите")
    with db.session(db_path) as conn:
        order = db.get_order(conn, waiting)
        assert order["contact_reminder_stage"] == 1
        assert main.last_turn(db.get_lead(conn, order["lead_id"])["dialog_context"])[0] == "бот"
    assert asyncio.run(cr.run_once(now + timedelta(minutes=5))) == []  # повторно не шлём
