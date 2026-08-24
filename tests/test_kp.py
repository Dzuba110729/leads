import kp


def test_compute_matches_formula():
    # 2 ч/нед * 4 нед * 950 ₽/ч = 7600, + 15% накладные = 8740, + 25% наценка = 10925,
    # округление до 10 ₽ (банковское round-half-even) -> 10920
    price = kp.compute(hours_per_week=2, weeks=4, subjects=1)
    assert price == 10920.0


def test_estimate_from_dialog_without_llm_flags_needs_estimator(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    estimate = kp.estimate_from_dialog("нужен репетитор по математике, без деталей")
    assert estimate.needs_estimator
    assert estimate.price > 0
