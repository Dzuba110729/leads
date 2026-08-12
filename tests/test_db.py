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
