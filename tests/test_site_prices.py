import json

import catalog
import db
import site_prices as sp
from config import CONFIG


def _tab(n, body, cls="price-item__subtitle pricesale11 ur2 js-tab-hidden"):
    return f'<div id="tab-{n}" name="tabs" class="{cls}">{body}</div>'


def _sale(old, new):
    return f'<s style="color:#808080">{old}</s> {new} <small>&nbsp;руб./месяц</small>'


# Та же разметка, что на og1.ru/czena (2026-10-16): классы — ссылки #tab-N, цены — div id="tab-N"
PRICES_PAGE = (
    "<nav>Тариф «Школьный» в меню</nav>"
    + "".join(f'<a href="#tab-{g + 6}" class="tabs__link">{g} класс</a>' for g in range(3, 12))
    + "<h3>Тариф «Пробный день»</h3>Бесплатно"
    + "<h3>Тариф «Ученический»</h3>"
    + "".join(_tab(n, _sale("12 800", "11 520")) for n in (11, 12, 13, 14))
    + _tab(15, _sale("17 000", "15 300")) + _tab(16, _sale("18 800", "16 920")) + _tab(17, _sale("24 500", "22 050"))
    + _tab(11, "* Преподавание ведется в московском часовом поясе.", cls="pricesale11 ur2") + "до 31.10.2026"
    + "<h3>Тариф «Школьный»</h3>"
    + _tab(9, "от 28 300 руб./месяц") + _tab(10, "от 28 300 руб./месяц")
    + "".join(_tab(n, _sale("33 700", "30 330")) for n in range(11, 18)) + "до 31.10.2026"
    + "<h3>«Аттестация»</h3><div>15 000 руб.</div><p>Прикрепление на время аттестации*.</p>"
      "<p>* Прикрепление к ЧОУ осуществляется с 5 по 11 класс.</p><p>** За 9 и 11 класс аттестация не проводится.</p>"
    + "<h3>Тариф «Любознательный»</h3>500 124 руб./месяц"
)

PROMOS_PAGE = (
    '<a href="#now">Действующие</a><div class="tabs-content courses__tabs-content">'
    '<div id="now" class="tabs-content__item row">'
    '<div class="col-lg-6 p20 courses_act"><div class="h2_title">Скидка 10% ко Дню учителя</div>'
    '<div class="link__ico"><span>Хотите начать учебный год выгодно?</span></div>'
    '<a href="https://og1.ru/akczi/teacher-day-2026" class="b">Подробнее</a></div>'
    '<div class="col-lg-6 p20 courses_act"><div class="h2_title">СКИДКА 10% ЗА ВТОРОГО РЕБЕНКА</div>'
    '<div class="link__ico"><span>Семейная школа</span></div>'
    '<a href="https://og1.ru/akczi/skidka" class="b">Подробнее</a></div>'
    '</div><div id="shcool" class="tabs-content__item row">'
    '<div class="col-lg-6 p20 courses_act"><div class="h2_title">Старая акция</div></div></div></div>'
)


def test_parses_grade_prices_sale_and_attestation():
    prices = sp.parse_prices(PRICES_PAGE)
    assert sp.validate(prices) is None
    assert prices["grades"]["Ученический"][5] == {"price": 12800, "sale": 11520, "from": False}
    assert prices["grades"]["Ученический"][11]["sale"] == 22050
    assert 3 not in prices["grades"]["Ученический"]  # «Ученический» с 5 класса
    assert prices["grades"]["Школьный"][3] == {"price": 28300, "sale": None, "from": True}
    assert prices["sale_until"] == "31.10.2026"
    assert prices["attestation"]["price"] == 15000
    assert "За 9 и 11 класс аттестация не проводится." in prices["attestation"]["notes"]


def test_parses_only_active_promos_and_details():
    promos = sp.parse_promos(PROMOS_PAGE)
    assert [p["title"] for p in promos] == ["Скидка 10% ко Дню учителя", "СКИДКА 10% ЗА ВТОРОГО РЕБЕНКА"]
    assert promos[0]["url"] == "https://og1.ru/akczi/teacher-day-2026"
    page = ("<div>Главная » Акции » Скидка 10% ко Дню учителя</div><p>Когда: с 5 по 31 октября.</p>"
            "<p>*Все подробности акции уточняйте у операторов</p><h2>Читайте также по теме</h2>Другое")
    assert sp.parse_promo_details(page, promos[0]["title"]) == "Когда: с 5 по 31 октября."


def test_broken_page_is_rejected():
    assert sp.validate(sp.parse_prices("<h3>Тариф «Школьный»</h3> скоро")) is not None
    weird = PRICES_PAGE.replace("17 000", "17").replace("15 300", "15")
    assert "странная цена" in sp.validate(sp.parse_prices(weird))


def _stored(prices, promos):
    prices["grades"] = {t: {str(g): p for g, p in grades.items()} for t, grades in prices["grades"].items()}
    return {**prices, "promos": promos, "fetched_at": "2026-10-16T08:00:00+00:00"}


def test_changes_listed_for_operators():
    old = _stored(sp.parse_prices(PRICES_PAGE), [{"title": "Скидка 10% ко Дню учителя"}])
    new = _stored(sp.parse_prices(PRICES_PAGE.replace("24 500", "25 900").replace("22 050", "23 310")),
                  [{"title": "Черная пятница"}])
    changes = sp.describe_changes(old, new)
    assert "«Ученический», 11 класс: 22 050 ₽/мес со скидкой (без скидки 24 500) → 23 310 ₽/мес со скидкой (без скидки 25 900)" in changes
    assert "Новая акция: Черная пятница" in changes and "Акция снята с сайта: Скидка 10% ко Дню учителя" in changes
    assert sp.describe_changes(old, old) == []


def test_bot_prompt_and_prices_come_from_site_when_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "db_path", str(tmp_path / "p.db"))
    assert "Ученический" in catalog.tariff_price_summary()  # ещё не читали сайт — запасной прайс
    data = _stored(sp.parse_prices(PRICES_PAGE), [{"title": "Скидка 10% ко Дню учителя", "details": "до 31 октября"}])
    with db.session() as conn:
        db.set_meta(conn, sp.META_KEY, json.dumps(data, ensure_ascii=False))
    summary = catalog.tariff_price_summary()
    assert "11 класс: 22 050 ₽/мес со скидкой (без скидки 24 500)" in summary
    assert "скидка 10% действует до 31.10.2026" in summary
    assert "Скидка 10% ко Дню учителя: до 31 октября" in summary
    assert catalog.price_for_grade("Ученический", 9) == 15300
    assert catalog.current_min_price(catalog.find_tariff("Ученический")) == 11520
    assert catalog.current_min_price(catalog.find_tariff("аттестация")) == 15000
