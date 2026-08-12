"""Скрипт-хелпер для получения VK_ACCESS_TOKEN (Implicit Flow OAuth), по аналогии с login.py/
export_session.py для Telegram — см. .scratch/lead-catcher-auto/spec.md Open Question 1.

Нужен ОТДЕЛЬНЫЙ VK-аккаунт от боевого userbot'а продаж. Нужен свой VK-приложение (Standalone)
на vk.com/apps?act=manage — создайте одно и укажите его ID в VK_APP_ID (.env), это разово.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

from config import CONFIG

SCOPE = "wall"
API_VERSION = "5.199"


def build_auth_url(app_id: str) -> str:
    return (
        "https://oauth.vk.com/authorize?"
        f"client_id={app_id}&display=page&redirect_uri=https://oauth.vk.com/blank.html"
        f"&scope={SCOPE}&response_type=token&v={API_VERSION}"
    )


def extract_token(redirected_url: str) -> str:
    parsed = urlparse(redirected_url.replace("#", "?", 1))
    params = dict(parse_qsl(parsed.query))
    token = params.get("access_token")
    if not token:
        raise ValueError("access_token не найден в ссылке — убедитесь, что скопировали адрес целиком после логина")
    return token


def main() -> None:
    import os

    app_id = os.getenv("VK_APP_ID", "")
    if not app_id:
        raise SystemExit(
            "Задайте VK_APP_ID в .env — id Standalone-приложения VK "
            "(создать: https://vk.com/apps?act=manage)"
        )

    print("1. Откройте эту ссылку в браузере ОТДЕЛЬНОГО VK-аккаунта для выгрузки (не боевого):")
    print(build_auth_url(app_id))
    print()
    print("2. После логина и разрешения доступа браузер перейдёт на пустую страницу oauth.vk.com/blank.html —")
    print("   скопируйте ПОЛНЫЙ адрес из адресной строки и вставьте сюда:")
    redirected_url = input("> ").strip()

    token = extract_token(redirected_url)
    print()
    print("VK_ACCESS_TOKEN=" + token)
    print("Скопируйте строку выше в .env. Обращайтесь с ней как с паролем.")


if __name__ == "__main__":
    main()
