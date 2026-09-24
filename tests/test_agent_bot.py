import asyncio
import json

import agent_bot
import catcher_db
import db
from config import CONFIG


class _Block:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


class _FakeClient:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _setup_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "a.db")
    monkeypatch.setattr(CONFIG, "db_path", db_path)
    with db.session(db_path) as conn:
        catcher_db.migrate(conn)
        src = catcher_db.add_source(conn, "tg", "https://t.me/parents_chat")
        mid = catcher_db.insert_raw_message(
            conn, source_chat_id=src["id"], external_id="1", author="Оля", text="ищем школу",
            url="https://t.me/parents_chat/1", posted_at="2026-09-20T10:00:00+00:00",
            author_username="olya", author_tg_id=7,
        )
        catcher_db.add_candidate(conn, raw_message_id=mid, quote="ищем школу", reason="ищет", contact_url=None, opener_text="Привет")
    return db_path


def test_tools_list_sources_and_stats(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    sources = json.loads(agent_bot._tool_list_sources())
    assert sources[0]["url"] == "https://t.me/parents_chat"
    stats = json.loads(agent_bot._tool_get_stats())
    assert stats["catcher_candidates"]["new"] == 1
    recent = json.loads(agent_bot._tool_list_recent_candidates(5, "new"))
    assert recent[0]["author"] == "@olya"


def test_answer_runs_tool_loop(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    script = [
        _Response([_Block("tool_use", id="t1", name="get_stats", input={})], "tool_use"),
        _Response([_Block("text", text="Новых кандидатов: 1")], "end_turn"),
    ]
    fake = _FakeClient(script)
    monkeypatch.setattr(agent_bot, "_client", fake)

    reply = asyncio.run(agent_bot.answer(_FakeBot(), 42, "сколько лидов?"))

    assert reply == "Новых кандидатов: 1"
    second_call = fake.messages.calls[1]["messages"]
    assert second_call[-1]["content"][0]["type"] == "tool_result"
    assert '"new": 1' in second_call[-1]["content"][0]["content"]
    with db.session() as conn:
        history = db.load_agent_history(conn, 42, 10)
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_start_catcher_run_sends_result_later(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    async def fake_run_sources(ids):
        return [agent_bot.catcher_service.SourceRunResult(1, "https://t.me/parents_chat", 12, 2)]

    monkeypatch.setattr(agent_bot.catcher_service, "run_sources", fake_run_sources)
    bot = _FakeBot()

    async def scenario():
        started = json.loads(agent_bot._tool_start_catcher_run(bot, 42, [], False))
        assert started["started"] is True
        busy = json.loads(agent_bot._tool_start_catcher_run(bot, 42, [], False))
        assert "error" in busy
        await agent_bot._running_jobs[42]

    asyncio.run(scenario())
    assert bot.sent and "новых лидов 2" in bot.sent[0][1]
    assert 42 not in agent_bot._running_jobs


def test_export_requires_google_config(monkeypatch):
    monkeypatch.setattr(agent_bot.gdocs, "available", lambda: False)
    result = json.loads(agent_bot._tool_start_catcher_run(_FakeBot(), 1, [], True))
    assert "Google Docs не настроен" in result["error"]
