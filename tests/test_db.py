import sqlite3

import db


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_migrate_is_idempotent():
    conn = _mem_conn()
    db.migrate(conn)
    db.migrate(conn)  # second call must not raise
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"leads", "orders", "handoff", "meta"} <= tables


def test_get_or_create_lead_roundtrip():
    conn = _mem_conn()
    db.migrate(conn)
    lead1 = db.get_or_create_lead(conn, tg_id=111, username="parent1")
    lead2 = db.get_or_create_lead(conn, tg_id=111, username="parent1")
    assert lead1["id"] == lead2["id"]
    assert lead1["blocked"] == 0


def test_order_status_progression():
    conn = _mem_conn()
    db.migrate(conn)
    lead = db.get_or_create_lead(conn, tg_id=222, username="parent2")
    order = db.create_order(conn, lead["id"], "Школьный", 28310, "приёмная_комиссия", False, "тест")
    assert order["status"] == "заявка"
    for expected in ("пробный_день", "документы", "договор", "оплачено", "зачислен"):
        new_status = db.advance_status(conn, order["id"])
        assert new_status == expected
    # дальше не двигается за пределы последнего статуса
    assert db.advance_status(conn, order["id"]) == "зачислен"
    final = db.get_order(conn, order["id"])
    assert final["closed"] == 1


def test_reset_clears_tables():
    conn = _mem_conn()
    db.migrate(conn)
    db.get_or_create_lead(conn, tg_id=333, username="parent3")
    db.reset(conn)
    assert conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"] == 0


def test_order_visible_only_to_owner():
    conn = _mem_conn()
    db.migrate(conn)
    owner = db.get_or_create_lead(conn, tg_id=444, username="owner")
    order = db.create_order(conn, owner["id"], "Школьный", 28310, "приёмная_комиссия", False, "тест")
    assert db.get_order_owned_by(conn, order["id"], 444)["id"] == order["id"]
    assert db.get_order_owned_by(conn, order["id"], 555) is None
    assert db.get_order_owned_by(conn, 999, 444) is None


def test_block_and_unblock_lead():
    conn = _mem_conn()
    db.migrate(conn)
    lead = db.get_or_create_lead(conn, tg_id=666, username="parent6")
    db.block_lead(conn, lead["id"], "тестовая причина")
    blocked = db.list_blocked_leads(conn)
    assert [l["id"] for l in blocked] == [lead["id"]]
    assert blocked[0]["block_reason"] == "тестовая причина"
    db.unblock_lead(conn, lead["id"])
    assert db.list_blocked_leads(conn) == []
    refreshed = db.get_or_create_lead(conn, tg_id=666, username="parent6")
    assert refreshed["blocked"] == 0
    assert refreshed["block_reason"] is None
