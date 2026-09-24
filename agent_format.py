"""Форматирование ответов ТГ-агента для Telegram.

Модель пишет в облегчённом markdown (**жирный**, `код`, [текст](ссылка), списки), а Telegram
понимает только свой HTML-поднабор — переводим сами, экранируя всё остальное, чтобы случайный
символ «<» в ответе не ломал отправку.
"""
from __future__ import annotations

import html
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

TELEGRAM_CHUNK = 3500  # с запасом до лимита 4096: после перевода в HTML текст удлиняется

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)\"<>]+)\)")
_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*|__([^_\n]+?)__")
_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
_HRULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)


def md_to_html(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _HRULE.sub("", out)
    out = _HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    out = _BULLET.sub(lambda m: f"{m.group(1)}• ", out)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def split_text(text: str, limit: int = TELEGRAM_CHUNK) -> list[str]:
    """Режем по абзацам, затем по строкам — чтобы разметка не рвалась посередине."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if c.strip()] or [text[:limit] or "…"]


async def send_markdown(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await bot.send_message(
                chat_id, md_to_html(chunk), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
            )
        except TelegramBadRequest:
            await bot.send_message(chat_id, chunk, disable_web_page_preview=True, reply_markup=markup)
