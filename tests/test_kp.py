import kp


def test_compute_matches_formula():
    # 2 ч/нед * 4 нед * 1500 ₽/ч = 12000, + 15% накладные = 13800, + 25% наценка = 17250
    price = kp.compute(hours_per_week=2, weeks=4, subjects=1)
    assert price == 17250.0


def test_estimate_from_dialog_without_llm_flags_needs_estimator(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    estimate = kp.estimate_from_dialog("нужен репетитор по математике, без деталей")
    assert estimate.needs_estimator
    assert estimate.price > 0
