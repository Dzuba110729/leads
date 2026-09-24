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
