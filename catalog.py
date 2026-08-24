"""Прайс og1 (см. og1/PLAN.md п.1) + маршрутизация заявки в отдел.

Цены — реальные факты с сайта og1.ru. Сетка по классам для "Школьный"/"Ученический" сверена
2026-08-12 с og1.ru/tarif-shkolniy и og1.ru/tarif-uchenicheskij (см. GRADE_PRICING,
БРИФ_OG1_ЗАПОЛНЕННЫЙ.md п.1.3). Ставка репетиторов для движка КП (kp.py) — подтверждена
заказчиком 2026-08-12: чёткой сетки по предметам/уровням нет, единая ставка "от 950 ₽/час".
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
    Tariff("Школьный", "очно, живые уроки", 28310, 38850, False, "приёмная_комиссия"),
    Tariff("Ученический", "очно-заочно, потоковые лекции", 12050, 23100, False, "приёмная_комиссия"),
    Tariff("Любознательный", "самоподготовка", 124, 500, False, "приёмная_комиссия"),
    Tariff("Аттестация", "прикрепление для аттестации", 15000, 15000, True, "приёмная_комиссия"),
    Tariff("Индивидуальные репетиторы", "подготовка к экзаменам", None, None, False, "репетиторы"),
]

# Полная сетка цен по классам (₽/мес), og1.ru/czena, сверено 2026-08-12 (см. БРИФ_OG1_ЗАПОЛНЕННЫЙ.md п.1.3)
GRADE_PRICING: dict[str, dict[int, float]] = {
    "Школьный": {
        3: 28310, 4: 31250, 5: 31800, 6: 31800, 7: 31800, 8: 31800,
        9: 34650, 10: 34650, 11: 38850,
    },
    # "Ученический" не предлагается для 3-4 класса
    "Ученический": {
        5: 12050, 6: 12050, 7: 12050, 8: 12050, 9: 16050, 10: 17750, 11: 23100,
    },
}


def price_for_grade(tariff_name: str, grade: int) -> float | None:
    """Точная цена по классу для тарифов с сеткой (Школьный/Ученический). None, если не найдено."""
    return GRADE_PRICING.get(tariff_name, {}).get(grade)


# Ставка для движка КП (kp.py) — от 950 ₽/час, подтверждено заказчиком, единая на все предметы/уровни
TUTOR_RATE_PLACEHOLDER = 950.0  # ₽/час


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


def tariff_price_summary() -> str:
    """Короткая сводка тарифов+цен для промпта (см. БРИФ_OG1_ЗАПОЛНЕННЫЙ.md п.1.3) -
    чтобы бот называл реальные цифры лиду, а не отправлял к менеджеру за любой цифрой."""
    lines = []
    for tariff in TARIFFS:
        lines.append(f"- {tariff.name} ({tariff.format}): {tariff.price_label()}")
    return "\n".join(lines)
