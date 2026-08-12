"""Прайс og1 (см. og1/PLAN.md п.1) + маршрутизация заявки в отдел.

Цены — реальные факты с сайта og1.ru на момент написания плана (2026-08-10), не выдумка.
Ставки репетиторов для движка КП (kp.py) — ПЛЕЙСХОЛДЕР, реальных ставок пользователь
не предоставлял; заменить в TUTOR_RATE_PLACEHOLDER, когда придут боевые данные.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tariff:
    name: str
    format: str
    price_min: float | None
    price_max: float | None
    is_fixed: bool  # разовая оплата (Аттестация) vs. ежемесячная подписка
    department: str  # 'приёмная_комиссия' | 'репетиторы'

    def price_label(self) -> str:
        if self.price_min is None:
            return "по запросу"
        if self.price_max is not None and self.price_max != self.price_min:
            return f"{self.price_min:,.0f}–{self.price_max:,.0f} ₽".replace(",", " ")
        unit = "" if self.is_fixed else "/мес"
        return f"от {self.price_min:,.0f} ₽{unit}".replace(",", " ")


TARIFFS: list[Tariff] = [
    Tariff("Школьный", "очно, живые уроки", 28310, None, False, "приёмная_комиссия"),
    Tariff("Ученический", "очно-заочно, потоковые лекции", 0, 23100, False, "приёмная_комиссия"),
    Tariff("Любознательный", "самоподготовка", 124, 500, False, "приёмная_комиссия"),
    Tariff("Аттестация", "прикрепление для аттестации", 15000, 15000, True, "приёмная_комиссия"),
    Tariff("Индивидуальные репетиторы", "подготовка к экзаменам", None, None, False, "репетиторы"),
]

# Плейсхолдер-ставка для движка КП (kp.py) до получения боевого справочника ставок
TUTOR_RATE_PLACEHOLDER = 1500.0  # ₽/час


def find_tariff(text: str) -> Tariff | None:
    """Грубый матч по названию/ключевым словам. LLM-матч (кастомный кейс) — в kp.py."""
    lowered = text.lower()
    for tariff in TARIFFS:
        if tariff.name.lower() in lowered:
            return tariff
    if any(w in lowered for w in ("репетитор", "индивидуальн", "подготовка к егэ", "подготовка к огэ")):
        return next(t for t in TARIFFS if t.name == "Индивидуальные репетиторы")
    if "аттестац" in lowered or "экстерн" in lowered:
        return next(t for t in TARIFFS if t.name == "Аттестация")
    return None


def route_department(tariff: Tariff | None) -> str:
    if tariff is None:
        return "приёмная_комиссия"
    return tariff.department
