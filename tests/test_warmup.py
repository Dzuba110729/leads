import asyncio
from datetime import datetime, timedelta, timezone

import db
import main
from config import CONFIG


class _FakeUserbot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, tg_id: int, text: str) -> None:
        self.sent.append((tg_id, text))


def test_warmup_silence_days():
    assert main.warmup_silence_days(None) is None
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3, hours=1)).isoformat()
    assert main.warmup_silence_days(three_days_ago) == 3


def test_warmup_once_sends_and_advances_step(tmp_path, monkeypatch):
    db_path = str(tmp_path / "w.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    monkeypatch.setattr(CONFIG, "dry_run", False)
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=301, username="quiet")
        db.record_touch(conn, lead["id"], next_step_idx=0)
        stale = (datetime.now(timezone.utc) - timedelta(days=1, hours=2)).isoformat()
        conn.execute("UPDATE leads SET last_touch_at = ? WHERE id = ?", (stale, lead["id"]))
        fresh = db.get_or_create_lead(conn, tg_id=302, username="fresh")
        db.record_touch(conn, fresh["id"], next_step_idx=0)

    userbot = _FakeUserbot()
    sent = asyncio.run(main.warmup_once(userbot))

    assert sent == 1
    assert [tg for tg, _ in userbot.sent] == [301]
    with db.session(db_path) as conn:
        row = conn.execute("SELECT next_step_idx, dialog_context FROM leads WHERE tg_id = 301").fetchone()
    assert row["next_step_idx"] == 1
    assert row["dialog_context"].startswith("бот: ")


def test_warmup_once_dry_run_does_not_send(tmp_path, monkeypatch):
    db_path = str(tmp_path / "w.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    monkeypatch.setattr(CONFIG, "dry_run", True)
    with db.session(db_path) as conn:
        lead = db.get_or_create_lead(conn, tg_id=303, username=None)
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn.execute("UPDATE leads SET last_touch_at = ? WHERE id = ?", (stale, lead["id"]))

    userbot = _FakeUserbot()
    assert asyncio.run(main.warmup_once(userbot)) == 1
    assert userbot.sent == []
