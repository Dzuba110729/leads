"""Отправка готового лида в Megabitra (AlterCPA) — API вебмастера `push.json`.

Вызывается вместе с карточкой в ленте готовых лидов (main._take_contact_if_awaited): лид дал
контакт и готов к связи. Документация: api-megabitra.md (в .gitignore — там ключ).
Обязательные поля API: flow, offer, ip, phone (международный формат, только цифры).
У лида из Telegram нет своего IP — передаём внешний IP компьютера с ботом (MEGABITRA_LEAD_IP=auto)
или заданный адрес. Лид без телефона (выбрал тг/почту) уходит с заглушкой MEGABITRA_NO_PHONE,
способ связи — в комментарии (решение владельца 2026-09-28).
"""
from __future__ import annotations

import logging
import re

import httpx

import db
import pipeline
from config import CONFIG

logger = logging.getLogger(__name__)

PUSH_URL = "https://megabitra.ru/api/wm/push.json"
COMMENT_LIMIT = 1500

ERRORS = {
    "access": "отправка по API не включена — нужен личный менеджер",
    "key": "неверный API-ключ",
    "noflow": "не указан поток", "badflow": "неверный поток", "nooffer": "не указан оффер",
    "offer": "оффер не найден", "no-api": "оффер не принимает лиды по API",
    "nophone": "нет телефона", "phone": "телефон не прошёл проверку", "email": "почта не прошла проверку",
    "duplicate": "такой лид уже есть в Megabitra", "ban": "телефон или IP в чёрном списке",
    "traffic": "оффер недоступен", "data": "не хватает обязательных полей", "db": "сбой Megabitra, повторите",
    "security": "аккаунт заблокирован",
}


def enabled() -> bool:
    return bool(CONFIG.megabitra_api_key and CONFIG.megabitra_offer and CONFIG.megabitra_flow and CONFIG.megabitra_lead_ip)


_ip_cache: tuple[float, str] | None = None
IP_CACHE_SECONDS = 1800


async def lead_ip() -> str | None:
    """MEGABITRA_LEAD_IP или, при auto, текущий внешний IP этого компьютера (домашний может меняться)."""
    global _ip_cache
    if CONFIG.megabitra_lead_ip != "auto":
        return CONFIG.megabitra_lead_ip
    import time

    if _ip_cache and time.time() - _ip_cache[0] < IP_CACHE_SECONDS:
        return _ip_cache[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            ip = (await client.get("https://api.ipify.org")).text.strip()
    except Exception as exc:
        logger.warning("megabitra: не узнал внешний IP: %s", exc)
        return _ip_cache[1] if _ip_cache else None
    _ip_cache = (time.time(), ip)
    return ip


def normalize_phone(raw: str | None) -> str | None:
    """«+7 (999) 123-45-67», «8 999 123 45 67» → 79991234567. Не телефон — None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]  # российский 8… → международный 7…
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits  # 999… без кода страны — Россия
    return digits if 10 <= len(digits) <= 15 else None


def _comment(order, lead) -> str:
    if db.is_vk_lead(lead):
        origin = f"Из VK ({db.vk_profile_url(lead)})"
    else:
        nick = f"@{lead['username']}" if lead["username"] else f"tg id {lead['tg_id']}"
        origin = f"Из Telegram ({nick})"
    parts = [
        f"{origin}, заявка og1 №{order['id']}.",
        f"Запрос: {order['summary'] or order['tariff']}.",
        f"Как связаться: {order['contact']}.",
    ]
    dialog = " / ".join(line.strip() for line in (lead["dialog_context"] or "").splitlines() if line.strip())
    text = " ".join(parts)
    room = COMMENT_LIMIT - len(text) - 12
    if dialog and room > 100:
        text += " Переписка: " + (dialog if len(dialog) <= room else "…" + dialog[-room:])
    return text


def build_payload(order, lead, ip: str) -> dict | None:
    """Поля для push.json. Без телефона — заглушка MEGABITRA_NO_PHONE, а если её нет — None."""
    phone = normalize_phone(pipeline.extract_phone(order["contact"] or "")) or normalize_phone(CONFIG.megabitra_no_phone)
    if phone is None:
        return None
    payload = {
        "flow": CONFIG.megabitra_flow,
        "offer": CONFIG.megabitra_offer,
        "ip": ip,
        "phone": phone,
        "name": lead["name"] or lead["username"] or "",
        "comment": _comment(order, lead),
        "utm_source": "telegram",
        "utm_medium": "og1_bot",
        "subid": f"og1-{order['id']}",
        "sub1": str(order["id"]),
    }
    email = pipeline.EMAIL_RE.search(order["contact"] or "")
    if email:
        payload["email"] = email.group()
    if CONFIG.megabitra_country:
        payload["country"] = CONFIG.megabitra_country
    return payload


def describe(result: dict) -> str:
    if result.get("status") == "ok":
        return f"лид #{result.get('id')} принят"
    code = result.get("error") or "ошибка"
    text = ERRORS.get(code, code)
    if result.get("bad"):
        text += f" ({result['bad']})"
    if result.get("info"):
        text += f" [{result['info']}]"
    return f"не отправлен — {text}"


async def push_lead(order, lead) -> dict:
    """Отправляет лида. Всегда возвращает dict со status ok/error/skip — сбой сети лид не теряет:
    карточка в ленте уходит в любом случае, отправить можно повторно."""
    ip = await lead_ip()
    if not ip:
        return {"status": "error", "error": "не удалось узнать IP"}
    payload = build_payload(order, lead, ip)
    if payload is None:
        return {"status": "skip", "error": "nophone"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(PUSH_URL, params={"id": CONFIG.megabitra_api_key}, data=payload)
        result = resp.json()
    except Exception as exc:
        logger.warning("megabitra: заявка %s не отправлена: %s", order["id"], exc)
        return {"status": "error", "error": f"сеть: {exc}"}
    logger.info("megabitra: заявка %s → %s", order["id"], result)
    return result if isinstance(result, dict) else {"status": "error", "error": str(result)[:200]}
