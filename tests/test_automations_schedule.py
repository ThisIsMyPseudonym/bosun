"""Tests for bosun.automations.schedule: local-time slots across DST and missed-slot catch-up."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bosun.automations.schedule import MISSED_WINDOW_S, due_slots, next_due
from bosun.automations.schema import ScheduleTrigger

CHI = ZoneInfo("America/Chicago")


def _ts(y: int, m: int, d: int, h: int, mi: int, fold: int = 0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=CHI, fold=fold).timestamp()


def test_schedule_handles_catchup_missed_weekdays_and_dst_transitions():
    daily = ScheduleTrigger(kind="schedule", time="02:00", days=())
    # Before the scheduled time: not due yet
    assert due_slots(daily, now=_ts(2026, 11, 27, 1, 59), tz=CHI) == ()
    # 90 minutes late (< 2 h): fires once
    slots = due_slots(daily, now=_ts(2026, 11, 27, 3, 30), tz=CHI)
    assert [(s.slot_key, s.missed) for s in slots] == [("2026-11-27T02:00", False)]
    # 3 hours late (>= 2 h): recorded as missed
    slots = due_slots(daily, now=_ts(2026, 11, 27, 5, 1), tz=CHI)
    assert [(s.slot_key, s.missed) for s in slots] == [("2026-11-27T02:00", True)]
    assert MISSED_WINDOW_S == 7200
    # Weekday filter: 2026-11-27 is Friday
    mon_only = ScheduleTrigger(kind="schedule", time="02:00", days=("mon",))
    assert due_slots(mon_only, now=_ts(2026, 11, 27, 2, 10), tz=CHI) == ()
    fri_only = ScheduleTrigger(kind="schedule", time="02:00", days=("fri",))
    assert [(s.slot_key, s.missed) for s in due_slots(fri_only, now=_ts(2026, 11, 27, 2, 10), tz=CHI)] == [
        ("2026-11-27T02:00", False)
    ]
    # DST spring-forward (2026-03-08 02:00 jumps to 03:00 in America/Chicago): fires once at 03:05
    spring = ScheduleTrigger(kind="schedule", time="02:30", days=())
    assert due_slots(spring, now=_ts(2026, 3, 8, 1, 55), tz=CHI) == ()
    got_spring = due_slots(spring, now=_ts(2026, 3, 8, 3, 5), tz=CHI)
    assert [(s.slot_key, s.missed) for s in got_spring] == [("2026-03-08T02:30", False)]
    # DST fall-back (2026-11-01 01:30 occurs twice): same local date slot key, never doubled
    fall = ScheduleTrigger(kind="schedule", time="01:30", days=())
    first = due_slots(fall, now=_ts(2026, 11, 1, 1, 35, fold=0), tz=CHI)
    second = due_slots(fall, now=_ts(2026, 11, 1, 1, 35, fold=1), tz=CHI)
    assert first[0].slot_key == second[0].slot_key == "2026-11-01T01:30"
    assert not first[0].missed and not second[0].missed


def test_next_due_finds_upcoming_local_slot():
    daily = ScheduleTrigger(kind="schedule", time="02:00", days=())
    # Before 02:00 on Nov 27 -> today at 02:00
    nxt = next_due(daily, now=_ts(2026, 11, 27, 1, 0), tz=CHI)
    assert nxt == _ts(2026, 11, 27, 2, 0)
    # After 02:00 on Nov 27 -> tomorrow at 02:00
    nxt2 = next_due(daily, now=_ts(2026, 11, 27, 2, 1), tz=CHI)
    assert nxt2 == _ts(2026, 11, 28, 2, 0)
    # Weekday filter: Friday 2026-11-27 -> next Monday 2026-11-30
    mon = ScheduleTrigger(kind="schedule", time="02:00", days=("mon",))
    assert next_due(mon, now=_ts(2026, 11, 27, 1, 0), tz=CHI) == _ts(2026, 11, 30, 2, 0)
