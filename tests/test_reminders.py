import reminders


def test_no_action_before_first_threshold():
    action, stage = reminders.compute_next_action(elapsed_minutes=10, current_stage=0)
    assert action is None
    assert stage == 0


def test_first_reminder_at_30_minutes():
    action, stage = reminders.compute_next_action(elapsed_minutes=31, current_stage=0)
    assert action == "remind"
    assert stage == 1


def test_no_repeat_within_same_stage():
    action, stage = reminders.compute_next_action(elapsed_minutes=45, current_stage=1)
    assert action is None
    assert stage == 1


def test_second_reminder_at_1_day():
    action, stage = reminders.compute_next_action(elapsed_minutes=60 * 24 + 5, current_stage=1)
    assert action == "remind"
    assert stage == 2


def test_third_reminder_at_3_days():
    action, stage = reminders.compute_next_action(elapsed_minutes=60 * 24 * 3 + 5, current_stage=2)
    assert action == "remind"
    assert stage == 3


def test_autoclose_after_3_days_30_min():
    action, stage = reminders.compute_next_action(elapsed_minutes=60 * 24 * 3 + 31, current_stage=3)
    assert action == "autoclose"
