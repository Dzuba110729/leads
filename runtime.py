"""Живое состояние процесса для раздела «Система» ТГ-агента: заполняется в main.py при старте."""
from __future__ import annotations

from datetime import datetime, timezone

started_at = datetime.now(timezone.utc)
userbot = None  # telethon.TelegramClient продающего аккаунта или None
cashier_bot_enabled = False
