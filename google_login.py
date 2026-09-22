"""Разовый вход в Google от имени владельца: открывает браузер, сохраняет токен для gdocs.py.

Нужен OAuth-клиент типа «Desktop app» (GOOGLE_OAUTH_CLIENT_FILE). Токен обновляется сам,
повторный вход нужен только если его отозвать в настройках Google-аккаунта.
"""
from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

import gdocs
from config import CONFIG


def main() -> None:
    if not CONFIG.google_oauth_client_file:
        sys.exit("В .env не задан GOOGLE_OAUTH_CLIENT_FILE (файл client_secret_*.json типа Desktop)")
    flow = InstalledAppFlow.from_client_secrets_file(CONFIG.google_oauth_client_file, gdocs.SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path = Path(CONFIG.google_oauth_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Готово: токен сохранён в {token_path}")


if __name__ == "__main__":
    main()
