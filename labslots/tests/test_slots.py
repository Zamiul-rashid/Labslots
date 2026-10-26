from datetime import date, datetime

from labslots.slots import Slot, booking_covers, in_switch_window, monday_of, next_slot, slot_at, span, week_slots


def test_slot_at_boundaries():
    assert slot_at(datetime(2026, 10, 19, 5, 59)) == Slot(date(2026, 10, 18), "night")
    assert slot_at(datetime(2026, 10, 19, 6, 0)) == Slot(date(2026, 10, 19), "day")
    assert slot_at(datetime(2026, 10, 19, 17, 59)) == Slot(date(2026, 10, 19), "day")
    assert slot_at(datetime(2026, 10, 19, 18, 0)) == Slot(date(2026, 10, 19), "night")


def test_night_slot_spans_midnight():
    s = slot_at(datetime(2026, 10, 20, 3, 0))
    assert s == Slot(date(2026, 10, 19), "night") and s.weekday == 0
    assert s.start == datetime(2026, 10, 19, 18) and s.end == datetime(2026, 10, 20, 5, 55)


def test_slots_end_five_minutes_before_the_next_starts():
    day = Slot(date(2026, 10, 19), "day")
    assert day.end == datetime(2026, 10, 19, 17, 55) and next_slot(day).start == datetime(2026, 10, 19, 18)
    assert not in_switch_window(datetime(2026, 10, 19, 17, 54))
    assert in_switch_window(datetime(2026, 10, 19, 17, 55)) and in_switch_window(datetime(2026, 10, 19, 17, 59))
    assert not in_switch_window(datetime(2026, 10, 19, 18, 0))
    assert in_switch_window(datetime(2026, 10, 20, 5, 57)) and not in_switch_window(datetime(2026, 10, 20, 6, 0))


def test_labels_show_the_slot_hours():
    assert span("day") == "06:00–17:55" and span("night") == "18:00–05:55"
    assert Slot(date(2026, 10, 19), "night").label == "Mon 19 Oct 18:00-05:55"


def test_next_slot_and_week():
    assert next_slot(Slot(date(2026, 10, 19), "day")) == Slot(date(2026, 10, 19), "night")
    assert next_slot(Slot(date(2026, 10, 25), "night")) == Slot(date(2026, 10, 26), "day")
    w = week_slots(monday_of(date(2026, 10, 22)))
    assert len(w) == 14 and w[0] == Slot(date(2026, 10, 19), "day") and w[-1].half == "night"


def test_booking_covers_respects_term_dates():
    s = Slot(date(2026, 10, 19), "day")
    assert booking_covers(0, "day", date(2026, 10, 1), date(2026, 12, 31), s)
    assert not booking_covers(0, "night", date(2026, 10, 1), date(2026, 12, 31), s)
    assert not booking_covers(0, "day", date(2026, 10, 20), date(2026, 12, 31), s)
