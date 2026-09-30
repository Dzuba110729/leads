"""Разовый ручной вход в VK для браузерного ловца (vk_browser.py), по аналогии с login.py.

Открывает обычное окно Chromium на профиле secrets/vk_browser_profile. Вы сами входите в VK
в этом окне — скрипт пароль не спрашивает и не видит, остаются только куки профиля. После
входа ловец читает VK-сообщества в фоне без токена. Профиль даёт доступ к аккаунту VK —
обращайтесь с папкой как с паролем.

Лучше входить ОТДЕЛЬНЫМ (запасным) VK-аккаунтом, не личным: VK может ограничить аккаунт,
который листает много сообществ.
"""
from __future__ import annotations

import vk_browser


def main() -> None:
    from playwright.sync_api import sync_playwright

    path = vk_browser.profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = vk_browser.open_context(p, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://vk.com/login", wait_until="domcontentloaded")
            print("Открылось окно браузера со страницей входа VK.")
            print("1. Войдите в VK в этом окне вручную (по возможности — отдельным, запасным аккаунтом).")
            print("   Пароль и коды вводите только в окне браузера, сюда ничего вводить не нужно.")
            print("2. Когда увидите свою ленту VK, вернитесь сюда и нажмите Enter.")
            input("> ")

            if not vk_browser.is_logged_in(page):
                raise SystemExit(
                    "Вход в VK не найден: страница ленты не открылась. Запустите скрипт ещё раз "
                    "и дождитесь своей ленты в окне браузера, прежде чем нажимать Enter."
                )
            user_agent = page.evaluate("navigator.userAgent")
            (path / vk_browser.LOGIN_MARKER).write_text(user_agent, encoding="utf-8")
        finally:
            context.close()
    print(f"Готово: вход в VK сохранён в {path}.")
    print("Ловец будет читать VK-сообщества через этот профиль. Бота перезапускать не нужно.")


if __name__ == "__main__":
    main()
