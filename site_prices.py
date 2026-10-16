"""Цены и акции с сайта og1.ru — бот берёт их оттуда раз в сутки, а не из зашитого в код прайса.

Источники:
- og1.ru/czena — тарифы по классам: у «Школьный»/«Ученический» блок цены на класс (<div id="tab-N">,
  N ↔ класс по ссылкам «#tab-N … K класс»), внутри <s>старая цена</s> и цена со скидкой; «Аттестация».
- og1.ru/akczii — вкладка «Действующие»: заголовок, описание и ссылка; условия — со страницы акции.

Разобрать страницу не вышло (сайт переверстали, цифры выглядят как мусор) — остаётся последняя удачная
версия, операторам уходит предупреждение. Изменения цен и акций операторам присылаются списком.
Хранится в meta["site_prices"] (JSON)."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

import db

logger = logging.getLogger(__name__)

PRICES_URL = "https://og1.ru/czena"
PROMOS_URL = "https://og1.ru/akczii"
META_KEY = "site_prices"
REFRESH_EVERY = timedelta(hours=23)
CHECK_EVERY_SECONDS = 3600
GRADE_TARIFFS = ("Школьный", "Ученический")
PROMO_DETAILS_LIMIT = 700


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _rub(value: str) -> int | None:
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else None


def _tab_grades(page: str) -> dict[str, int]:
    """«#tab-9» → 3 по ссылкам-переключателям классов."""
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r'href="#(tab-\d+)"[^>]*>\s*(\d{1,2})\s*класс', page)}


def _tariff_section(page: str, heading: str) -> str:
    """Карточка тарифа: от <h3>{heading}</h3> до заголовка следующей карточки."""
    start = page.find(f"<h3>{heading}</h3>")
    if start < 0:
        return ""
    end = page.find("<h3>", start + 4)
    return page[start:end if end > 0 else len(page)]


def parse_prices(page: str) -> dict:
    """Страница og1.ru/czena → {"grades": {тариф: {класс: {"price", "sale", "from"}}},
    "sale_until": "31.10.2026" | None, "attestation": {"price", "notes"}, "trial": "..."}."""
    grades_by_tab = _tab_grades(page)
    result: dict = {"grades": {}, "sale_until": None, "attestation": None}
    for title in GRADE_TARIFFS:
        section = _tariff_section(page, f"Тариф «{title}»")
        grades = {}
        for m in re.finditer(r'<div id="(tab-\d+)"[^>]*class="[^"]*price-item__subtitle[^"]*"[^>]*>(.*?)</div>',
                             section, re.S):
            grade = grades_by_tab.get(m.group(1))
            if grade is None:
                continue
            body = m.group(2)
            old = re.search(r"<s[^>]*>(.*?)</s>", body, re.S)
            rest = _text(re.sub(r"<s[^>]*>.*?</s>", " ", body, flags=re.S))
            current = _rub(rest.split("руб")[0])
            if current is None:
                continue
            if old:
                grades[grade] = {"price": _rub(_text(old.group(1))), "sale": current, "from": False}
            else:
                grades[grade] = {"price": current, "sale": None, "from": rest.lower().startswith("от")}
        result["grades"][title] = grades
        until = re.search(r"до\s+(\d{2}\.\d{2}\.\d{4})", _text(section))
        if until and not result["sale_until"]:
            result["sale_until"] = until.group(1)
    section = _tariff_section(page, "«Аттестация»")
    if section:
        price = re.search(r"(\d[\d\s]*)\s*руб", _text(section))
        # сноски «* Прикрепление … с 5 по 11 класс», «** За 9 и 11 класс аттестация не проводится»
        notes = [n.strip() for n in re.findall(r"\*+\s*([А-ЯA-Z][^*]{10,})", _text(section))]
        if price:
            result["attestation"] = {"price": _rub(price.group(1)), "notes": notes}
    return result


def parse_promos(page: str) -> list[dict]:
    """Вкладка «Действующие» на og1.ru/akczii → [{"title", "desc", "url"}]."""
    start = page.find('id="now"')
    if start < 0:
        return []
    end = page.find('class="tabs-content__item', start + 10)
    section = page[start:end if end > 0 else len(page)]
    promos = []
    for card in re.split(r'class="[^"]*courses_act', section)[1:]:
        title = re.search(r'h2_title">(.*?)</div>', card, re.S)
        if not title:
            continue
        desc = re.search(r'link__ico">\s*<span>(.*?)</span>', card, re.S)
        link = re.search(r'<a href="([^"]+)"[^>]*>\s*Подробнее', card)
        promos.append({"title": _text(title.group(1)), "desc": _text(desc.group(1)) if desc else "",
                       "url": link.group(1) if link else ""})
    return promos


def parse_promo_details(page: str, title: str = "") -> str:
    """Условия со страницы акции: текст между «Главная » …» и «Читайте также». title — заголовок
    акции из списка: в «хлебных крошках» он стоит первым, убираем."""
    text = _text(re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page))
    start = text.find("Главная »")
    if start < 0:
        return ""
    text = text[start:]
    end = text.find("Читайте также")
    text = text[:end if end > 0 else PROMO_DETAILS_LIMIT * 2]
    crumb = re.match(r"^Главная »\s*Акции »\s*", text)
    if crumb:
        text = text[crumb.end():]
        if title and text.lower().startswith(title.lower()):
            text = text[len(title):]
    text = re.sub(r"\*?Все подробности акции уточняйте.*$", "", text).strip()
    return text[:PROMO_DETAILS_LIMIT]


def validate(prices: dict) -> str | None:
    """Ошибка разбора или None. Не подставляем лиду мусор, если сайт переверстали."""
    for title in GRADE_TARIFFS:
        grades = prices.get("grades", {}).get(title) or {}
        if len(grades) < 5:
            return f"«{title}»: нашёл цены только для {len(grades)} классов"
        for grade, p in grades.items():
            for value in (p["price"], p["sale"]):
                if value is not None and not 3000 <= value <= 200000:
                    return f"«{title}», {grade} класс: странная цена {value}"
    att = prices.get("attestation")
    if not att or not 3000 <= (att.get("price") or 0) <= 200000:
        return "не нашёл цену «Аттестации»"
    return None


def _fmt(n: int | None) -> str:
    return f"{n:,}".replace(",", " ") if n is not None else "?"


def _grade_line(p: dict) -> str:
    if p["sale"]:
        return f"{_fmt(p['sale'])} ₽/мес со скидкой (без скидки {_fmt(p['price'])})"
    return f"{'от ' if p['from'] else ''}{_fmt(p['price'])} ₽/мес"


def describe_changes(old: dict | None, new: dict) -> list[str]:
    """Что поменялось на сайте с прошлого раза — строками для операторов."""
    if not old:
        return []
    changes = []
    for title in GRADE_TARIFFS:
        before, after = old.get("grades", {}).get(title, {}), new["grades"].get(title, {})
        for grade in sorted({int(g) for g in before} | {int(g) for g in after}):
            b, a = before.get(str(grade)) or before.get(grade), after.get(str(grade)) or after.get(grade)
            if b != a:
                changes.append(f"«{title}», {grade} класс: {_grade_line(b) if b else 'нет'} → {_grade_line(a) if a else 'нет'}")
    if old.get("sale_until") != new.get("sale_until"):
        changes.append(f"Скидка по тарифам до: {old.get('sale_until') or 'нет'} → {new.get('sale_until') or 'нет'}")
    ob, nb = (old.get("attestation") or {}).get("price"), (new.get("attestation") or {}).get("price")
    if ob != nb:
        changes.append(f"«Аттестация»: {_fmt(ob)} → {_fmt(nb)} ₽")
    old_titles = {p["title"] for p in old.get("promos", [])}
    new_titles = {p["title"] for p in new.get("promos", [])}
    changes += [f"Новая акция: {t}" for t in sorted(new_titles - old_titles)]
    changes += [f"Акция снята с сайта: {t}" for t in sorted(old_titles - new_titles)]
    return changes


def load() -> dict | None:
    with db.session() as conn:
        raw = db.get_meta(conn, META_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def summary_for_prompt(data: dict | None = None) -> str | None:
    """Цены по классам и акции для промпта бота. None — с сайта ещё ничего не получено."""
    data = data if data is not None else load()
    if not data:
        return None
    lines = []
    sale = f" (скидка 10% действует до {data['sale_until']})" if data.get("sale_until") else ""
    for title in GRADE_TARIFFS:
        grades = data["grades"].get(title) or {}
        if not grades:
            continue
        lines.append(f"- {title}, по классам{sale}:")
        for grade in sorted(grades, key=int):
            lines.append(f"  {grade} класс: {_grade_line(grades[grade])}")
    att = data.get("attestation") or {}
    if att:
        notes = " ".join(att.get("notes") or [])
        lines.append(f"- Аттестация (прикрепление для аттестации): {_fmt(att['price'])} ₽" + (f". {notes}" if notes else ""))
    lines.append("- Пробный день: бесплатно (3 живых урока с учителем)")
    lines.append("- Индивидуальные репетиторы: по запросу")
    promos = data.get("promos") or []
    if promos:
        lines.append("\nДЕЙСТВУЮЩИЕ АКЦИИ og1 (с сайта og1.ru/akczii; упоминай, только если к месту — "
                     "лид спросил о цене/скидках или подходит под условия; условия не додумывай):")
        for p in promos:
            details = p.get("details") or p.get("desc") or ""
            lines.append(f"- {p['title']}: {details}")
    return "\n".join(lines)


async def _get(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (og1 leads bot)"}, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


async def refresh() -> tuple[dict | None, list[str], str | None]:
    """Скачивает и разбирает сайт. → (новые данные или None, изменения, ошибка)."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            prices = parse_prices(await _get(client, PRICES_URL))
        except Exception as exc:
            return None, [], f"не открылась {PRICES_URL}: {exc}"
        error = validate(prices)
        if error:
            return None, [], f"{PRICES_URL}: {error}"
        try:
            promos = parse_promos(await _get(client, PROMOS_URL))
        except Exception as exc:
            logger.warning("site prices: акции не открылись: %s", exc)
            promos = (load() or {}).get("promos", [])
        for promo in promos:
            if "og1.ru/akczi/" in promo["url"] or "og1.ru/novosti/" in promo["url"]:
                try:
                    promo["details"] = parse_promo_details(await _get(client, promo["url"]), promo["title"])
                except Exception as exc:
                    logger.warning("site prices: акция %s не открылась: %s", promo["url"], exc)
    # JSON-ключи — строки: сравниваем и храним в одном виде
    prices["grades"] = {t: {str(g): p for g, p in grades.items()} for t, grades in prices["grades"].items()}
    data = {**prices, "promos": promos, "fetched_at": datetime.now(timezone.utc).isoformat()}
    old = load()
    changes = describe_changes(old, data)
    with db.session() as conn:
        db.set_meta(conn, META_KEY, json.dumps(data, ensure_ascii=False))
    return data, changes, None


def is_stale(data: dict | None, now: datetime | None = None) -> bool:
    if not data or not data.get("fetched_at"):
        return True
    return (now or datetime.now(timezone.utc)) - datetime.fromisoformat(data["fetched_at"]) >= REFRESH_EVERY


_error_notified_on: str | None = None  # о сбое разбора предупреждаем раз в день, а не каждый час


async def refresh_task() -> None:
    """Раз в час проверяет, не пора ли обновить (раз в сутки), и при запуске — сразу."""
    import asyncio

    import alerts

    while True:
        try:
            if is_stale(load()):
                data, changes, error = await refresh()
                global _error_notified_on
                today = datetime.now(timezone.utc).date().isoformat()
                if error:
                    logger.warning("site prices: %s", error)
                if error and _error_notified_on != today:
                    _error_notified_on = today
                    await alerts.notify_operators(
                        f"⚠️ Не смог обновить цены с сайта og1.ru: {error}\nБот пока называет прошлые цены.")
                elif not error:
                    logger.info("site prices: обновлено, изменений %s", len(changes))
                    if changes:
                        await alerts.notify_operators(
                            "💰 На сайте og1.ru изменились цены/акции — бот уже называет новые:\n"
                            + "\n".join(f"• {c}" for c in changes))
        except Exception:
            logger.exception("site prices refresh failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
