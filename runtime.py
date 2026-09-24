"""Живое состояние процесса для раздела «Система» ТГ-агента: заполняется в main.py при старте."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

started_at = datetime.now(timezone.utc)
userbot = None  # telethon.TelegramClient продающего аккаунта или None
cashier_bot_enabled = False

# Сообщения, которые продающий аккаунт отправил сам (ответ ИИ, прогрев, напоминание): (chat_id, msg_id).
# По ним обработчик исходящих отличает автоответ от сообщения, которое менеджер написал вручную.
auto_sent: deque[tuple[int, int]] = deque(maxlen=500)


def remember_auto_sent(chat_id: int, message) -> None:
    msg_id = getattr(message, "id", None)
    if msg_id is not None:
        auto_sent.append((int(chat_id), msg_id))


def is_auto_sent(chat_id: int, msg_id: int) -> bool:
    return (int(chat_id), msg_id) in auto_sent
