"""Slot arithmetic for the weekly schedule.

Every PC has 14 slots a week: a day slot 06:00-17:55 and a night slot
18:00-05:55 (next morning) on each weekday. A night slot belongs to the
date it starts on, so Monday's night slot runs Mon 18:00 -> Tue 05:55.
Each slot ends SWITCH_WINDOW before the next one starts; in that window no
group has access while the outgoing group is shut out and the incoming one
waits. All times are local (the TZ setting).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

DAY_START = 6
NIGHT_START = 18
SWITCH_WINDOW = timedelta(minutes=5)
HALVES = ("day", "night")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Slot:
    date: date
    half: str  # "day" | "night"

    @property
    def weekday(self) -> int:
        return self.date.weekday()

    @property
    def start(self) -> datetime:
        hour = DAY_START if self.half == "day" else NIGHT_START
        return datetime.combine(self.date, time(hour))

    @property
    def end(self) -> datetime:
        """When the group's access ends: SWITCH_WINDOW before the next slot starts."""
        return next_slot(self).start - SWITCH_WINDOW

    @property
    def label(self) -> str:
        return f"{WEEKDAYS[self.weekday]} {self.date:%d %b} {span(self.half, '-')}"


def span(half: str, sep: str = "–") -> str:
    """A half's hours, e.g. '06:00–17:55'."""
    s = Slot(date(2026, 1, 5), half)
    return f"{s.start:%H:%M}{sep}{s.end:%H:%M}"


def slot_at(now: datetime) -> Slot:
    """The slot that contains `now` (naive local time), its switch window included."""
    if now.hour >= NIGHT_START:
        return Slot(now.date(), "night")
    if now.hour < DAY_START:
        return Slot(now.date() - timedelta(days=1), "night")
    return Slot(now.date(), "day")


def in_switch_window(now: datetime) -> bool:
    """Between a slot's end and the next slot's start: no group has access."""
    return now >= slot_at(now).end


def next_slot(slot: Slot) -> Slot:
    if slot.half == "day":
        return Slot(slot.date, "night")
    return Slot(slot.date + timedelta(days=1), "day")


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_slots(monday: date) -> list[Slot]:
    """The 14 slots of the week starting on `monday`, in grid order."""
    return [Slot(monday + timedelta(days=i), half) for i in range(7) for half in HALVES]


def booking_covers(weekday: int, half: str, start: date, end: date, slot: Slot) -> bool:
    """Does a weekly booking (weekday/half within a term's dates) cover `slot`?"""
    return weekday == slot.weekday and half == slot.half and start <= slot.date <= end
