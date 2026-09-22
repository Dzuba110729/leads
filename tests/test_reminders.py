import reminders


def test_no_action_before_first_threshold():
    action, stage = reminders.compute_next_action(elapsed_minutes=10, current_stage=0)
    assert action is None
    assert stage == 0


def test_first_reminder_at_30_minutes():
    action, stage = reminders.compute_next_action(elapsed_minutes=31, current_stage=0)
    assert action == "remind"
    assert stage == 1


def test_no_repeat_within_same_stage():
    action, stage = reminders.compute_next_action(elapsed_minutes=45, current_stage=1)
    assert action is None
    assert stage == 1


def test_second_reminder_at_1_day():
    action, stage = reminders.compute_next_action(elapsed_minutes=60 * 24 + 5, current_stage=1)
    assert action == "remind"
    assert stage == 2


def test_third_reminder_at_3_days():
    action, stage = reminders.compute_next_action(elapsed_minutes=60 * 24 * 3 + 5, current_stage=2)
    assert action == "remind"
    assert stage == 3


def test_autoclose_after_3_days_30_min():
    action, stage = reminders.compute_next_action(elapsed_minutes=60 * 24 * 3 + 31, current_stage=3)
    assert action == "autoclose"


# --- устойчивость run_once: сбой отправки одному лиду не откатывает остальных ---------------

import asyncio
from datetime import datetime, timedelta, timezone

import db
from config import CONFIG


class _FakeBot:
    def __init__(self, fail_for: set[int]):
        self.fail_for = fail_for
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, tg_id: int, text: str) -> None:
        if tg_id in self.fail_for:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.sent.append((tg_id, text))


def _seed_orders(db_path: str, tg_ids: list[int]) -> list[int]:
    stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    order_ids = []
    with db.session(db_path) as conn:
        for tg_id in tg_ids:
            lead = db.get_or_create_lead(conn, tg_id=tg_id, username=None)
            order = db.create_order(conn, lead["id"], "Школьный", 28310, "приёмная_комиссия", False, "т")
            conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (stale, order["id"]))
            order_ids.append(order["id"])
    return order_ids


def test_run_once_skips_failed_lead_and_keeps_others(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    ids = _seed_orders(db_path, [101, 102, 103])
    bot = _FakeBot(fail_for={102})

    asyncio.run(reminders.run_once(bot))

    assert [tg for tg, _ in bot.sent] == [101, 103]
    with db.session(db_path) as conn:
        stages = {o["id"]: o["reminder_stage"] for o in conn.execute("SELECT id, reminder_stage FROM orders")}
    assert stages[ids[0]] == 1
    assert stages[ids[1]] == 0  # не отправили - не отмечаем
    assert stages[ids[2]] == 1


def test_run_once_falls_back_to_userbot(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    ids = _seed_orders(db_path, [201])
    bot = _FakeBot(fail_for={201})
    userbot = _FakeBot(fail_for=set())

    asyncio.run(reminders.run_once(bot, userbot))

    assert bot.sent == []
    assert [tg for tg, _ in userbot.sent] == [201]
    with db.session(db_path) as conn:
        assert db.get_order(conn, ids[0])["reminder_stage"] == 1
