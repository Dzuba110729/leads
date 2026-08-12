import pipeline


def test_band_for_score_boundaries():
    assert pipeline.band_for_score(0) == "cold"
    assert pipeline.band_for_score(19) == "cold"
    assert pipeline.band_for_score(20) == "warm_low"
    assert pipeline.band_for_score(39) == "warm_low"
    assert pipeline.band_for_score(40) == "warm"
    assert pipeline.band_for_score(59) == "warm"
    assert pipeline.band_for_score(60) == "hot"
    assert pipeline.band_for_score(79) == "hot"
    assert pipeline.band_for_score(80) == "very_hot"
    assert pipeline.band_for_score(100) == "very_hot"


def test_is_status_question():
    assert pipeline.is_status_question("а что со статусом моей заявки?")
    assert pipeline.is_status_question("где мой заказ")
    assert not pipeline.is_status_question("сколько стоит тариф школьный?")


def test_heuristic_score_no_llm(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    result = pipeline.score_message("готов начать, куда писать?")
    assert result.source == "heuristic"
    assert result.band == "very_hot"


def test_heuristic_score_no_match(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    result = pipeline.score_message("привет")
    assert result.score == 0
    assert result.band == "cold"


def test_extract_order_known_tariff():
    order = pipeline.extract_order("Хочу узнать про тариф Школьный, очные живые уроки")
    assert order.tariff_name == "Школьный"
    assert order.price == 28310
    assert order.department == "приёмная_комиссия"
    assert not order.needs_estimator


def test_extract_order_unknown_tariff():
    order = pipeline.extract_order("расскажите вообще о вас")
    assert order.tariff_name == "не определён"
    assert order.needs_estimator


def test_extract_order_tutor_routes_to_repetitors(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    order = pipeline.extract_order("нужен индивидуальный репетитор по математике")
    assert order.tariff_name == "Индивидуальные репетиторы"
    assert order.department == "репетиторы"
    assert order.price is not None
