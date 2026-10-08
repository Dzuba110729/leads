import asyncio

import anthropic

import alerts
import llm
import pipeline
from config import CONFIG


def _api_error():
    # Ошибка API без настоящего HTTP-ответа: конструктор требует response, нам важен только тип и текст
    exc = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
    Exception.__init__(exc, "Error code: 400 - Your credit balance is too low to access the Anthropic API.")
    return exc


class _Boom:
    class messages:
        @staticmethod
        def create(**kw):
            raise _api_error()


class _BadJson:
    class messages:
        @staticmethod
        def create(**kw):
            class B:
                type = "text"
                text = "не json"

            class R:
                content = [B()]

            return R()


def test_api_failure_is_outage_but_bad_json_is_not(monkeypatch):
    monkeypatch.setattr(CONFIG, "llm_api_key", "k")
    monkeypatch.setattr(llm, "_client", _Boom())
    touch, failed = alerts.run_llm_step(pipeline.generate_touch, "warm", "интерес", "лид: сколько стоит?")
    assert failed is True and touch  # шаблон подставлен, но main его не отправит
    assert "закончились деньги" in llm.describe_last_error()

    monkeypatch.setattr(llm, "_client", _BadJson())
    assert llm.call_json("s", "u") is None
    assert llm.thread_call_failed() is False


def test_no_key_is_not_outage(monkeypatch):
    monkeypatch.setattr(CONFIG, "llm_api_key", "")
    _, failed = alerts.run_llm_step(pipeline.generate_touch, "warm", "интерес", "лид: привет")
    assert failed is False and alerts.llm_configured() is False


def test_outage_alert_throttled_and_recovery_lists_missed(monkeypatch):
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(alerts, "notify_operators", fake_notify)
    monkeypatch.setattr(alerts, "_outage", False)
    monkeypatch.setattr(alerts, "_last_alert_at", None)
    monkeypatch.setattr(alerts, "_missed", [])
    llm.last_error = "Your credit balance is too low"

    async def scenario():
        await alerts.report_llm_outage("@a")
        await alerts.report_llm_outage("@b")  # в течение часа повторно не шлём
        await alerts.report_llm_ok()
        await alerts.report_llm_ok()  # сбоя больше нет — молчим

    asyncio.run(scenario())
    llm.last_error = None
    assert len(sent) == 2
    assert "ИИ недоступен" in sent[0] and "@a" in sent[0]
    assert "снова работает" in sent[1] and "@a, @b" in sent[1]


def _tmp_db(tmp_path, monkeypatch):
    import db

    path = str(tmp_path / "t.db")
    monkeypatch.setattr(CONFIG, "db_path", path)
    with db.session(path):
        pass
    return path


def test_missed_lead_survives_and_is_listed_once(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(alerts, "notify_operators", lambda text: asyncio.sleep(0))
    monkeypatch.setattr(alerts, "_outage", False)
    monkeypatch.setattr(alerts, "_last_alert_at", None)
    monkeypatch.setattr(alerts, "_missed", [])

    async def scenario():
        await alerts.report_llm_outage("@a", 1)
        await alerts.report_llm_outage("@a", 1)
        await alerts.report_llm_outage("@b", 2)

    asyncio.run(scenario())
    assert alerts.missed_leads() == [{"lead_id": 1, "who": "@a"}, {"lead_id": 2, "who": "@b"}]
    alerts.forget_missed(1)
    assert alerts.missed_leads() == [{"lead_id": 2, "who": "@b"}]


def test_undelivered_alert_is_resent_when_network_back(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    online = {"up": False}
    got = []

    async def fake_send(text):
        if online["up"]:
            got.append(text)
        return online["up"]

    monkeypatch.setattr(alerts, "_send_to_operators", fake_send)

    async def scenario():
        await alerts.notify_operators("сбой 1")
        await alerts.notify_operators("сбой 2")
        assert await alerts.resend_undelivered() == 0  # сети всё ещё нет — ничего не теряем
        online["up"] = True
        assert await alerts.resend_undelivered() == 2
        assert await alerts.resend_undelivered() == 0

    asyncio.run(scenario())
    assert len(got) == 2 and "сбой 1" in got[0] and "с опозданием" in got[0]


def test_last_turn_handles_multiline_reply():
    import main

    ctx = "менеджер: Добрый день! Рассказать?\nлид: Здравствуйте.\nРасскажите пожалуйста"
    assert main.last_turn(ctx) == ("лид", "Здравствуйте.\nРасскажите пожалуйста")
    assert main.last_turn(ctx + "\nбот: Конечно")[0] == "бот"
    assert main.last_turn("") == (None, "")


def test_retry_answers_only_leads_still_waiting(tmp_path, monkeypatch):
    import db
    import main

    path = _tmp_db(tmp_path, monkeypatch)
    with db.session(path) as conn:
        waiting = db.get_or_create_lead(conn, 101, "maria", source="dm")
        db.append_dialog_context(conn, waiting["id"], "менеджер", "Добрый день! Рассказать?")
        db.append_dialog_context(conn, waiting["id"], "лид", "Расскажите пожалуйста")
        answered = db.get_or_create_lead(conn, 102, "olga", source="dm")
        db.append_dialog_context(conn, answered["id"], "лид", "А цена?")
        db.append_dialog_context(conn, answered["id"], "менеджер", "Ответил вручную")
    alerts.remember_missed(waiting["id"], "@maria")
    alerts.remember_missed(answered["id"], "@olga")

    calls = []

    async def fake_process(event, lead, text, who, saved=False):
        calls.append((lead["tg_id"], text, saved, event.platform))
        with db.session(path) as conn:
            db.append_dialog_context(conn, lead["id"], "бот", "ответ")

    monkeypatch.setattr(main, "process_lead_text", fake_process)
    assert asyncio.run(main.retry_missed_replies()) == ["@maria"]
    assert calls == [(101, "Расскажите пожалуйста", True, "tg")]
    assert alerts.missed_leads() == []
