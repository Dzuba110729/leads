"""Вызов Claude через Claude Code CLI (`claude -p`) — за счёт подписки владельца, а не API.

Используется только ловцом лидов (разбор выгруженных чатов — внутренняя аналитика владельца,
запускается по его кнопке). Продающий бот, guard и ТГ-агент остаются на API (llm.py): им нужна
круглосуточная работа без лимитов подписки.

Ключи API из окружения намеренно убираются: если Claude Code увидит ANTHROPIC_API_KEY, он молча
пойдёт через API и спишет деньги с баланса, а не с подписки.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

from config import CONFIG

logger = logging.getLogger(__name__)

last_error: str | None = None

# Пустая рабочая папка: чтобы CLI не подхватывал CLAUDE.md, память и настройки проекта.
_WORKDIR = tempfile.mkdtemp(prefix="og1-catcher-cc-")


def _binary() -> str:
    return CONFIG.claude_bin or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_") and k != "LLM_API_KEY"}
    env.setdefault("HOME", os.path.expanduser("~"))
    return env


def available() -> bool:
    return os.path.exists(_binary())


def call_text(system_prompt: str, user_message: str, model: str = "sonnet", timeout: int = 600) -> str | None:
    """Ответ модели текстом или None при любом сбое (лимит подписки, нет CLI, таймаут)."""
    global last_error
    cmd = [
        _binary(), "-p",
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
    ]
    try:
        proc = subprocess.run(
            cmd, input=user_message, capture_output=True, text=True,
            timeout=timeout, cwd=_WORKDIR, env=_env(),
        )
    except FileNotFoundError:
        last_error = "Claude Code (claude) не найден на этом компьютере"
        logger.error(last_error)
        return None
    except subprocess.TimeoutExpired:
        last_error = f"Claude Code не ответил за {timeout} с"
        logger.error(last_error)
        return None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        last_error = (proc.stderr or proc.stdout or f"код выхода {proc.returncode}").strip()[:300]
        logger.error("claude -p: непонятный ответ: %s", last_error)
        return None
    if data.get("is_error"):
        last_error = str(data.get("result") or data.get("subtype") or "ошибка Claude Code")[:300]
        logger.error("claude -p: %s", last_error)
        return None
    last_error = None
    return str(data.get("result") or "").strip()


def describe_last_error() -> str | None:
    if not last_error:
        return None
    lowered = last_error.lower()
    if "limit" in lowered or "лимит" in lowered:
        return f"закончился лимит подписки Claude — разбор продолжится, когда лимит обновится ({last_error})"
    if "log in" in lowered or "login" in lowered or "auth" in lowered:
        return "Claude Code не авторизован — откройте терминал и выполните claude, войдите в аккаунт"
    return f"Claude Code: {last_error}"
