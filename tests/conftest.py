import pytest


@pytest.fixture(autouse=True)
def _no_real_claude_code(monkeypatch):
    """Тесты не должны запускать настоящий Claude Code CLI (тратит лимиты подписки)."""
    import claude_code

    monkeypatch.setattr(claude_code, "call_text", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _no_real_megabitra(monkeypatch):
    """Тесты не должны слать лидов в Megabitra: настройки из .env включают отправку,
    а тестовые лиды попадут в кабинет как настоящие (так уже случилось 2026-09-28)."""
    from config import CONFIG

    for key in ("megabitra_api_key", "megabitra_offer", "megabitra_flow", "megabitra_lead_ip"):
        monkeypatch.setattr(CONFIG, key, "")

    async def _blocked(*a, **kw):
        raise AssertionError("тест попытался отправить лида в Megabitra")

    import megabitra
    monkeypatch.setattr(megabitra, "push_lead", _blocked)
    monkeypatch.setattr(megabitra, "lead_ip", _blocked)
