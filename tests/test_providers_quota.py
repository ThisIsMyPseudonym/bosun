"""Staleness, share, pace, the 75% line and each call's draw on a usage window."""

import pytest

from bosun.providers import quota, windows

R = windows.Reading
NOW = 1_800_000_000.0


def test_a_reading_past_its_reset_or_undated_and_a_week_old_is_unknown():
    assert quota.stale(R("five_hour", "allowed", 0.3, NOW - 1, NOW - 100), NOW)
    assert quota.stale(R("five_hour", "allowed", 0.3, NOW, NOW - 100), NOW)  # at the reset itself: gone
    assert not quota.stale(R("five_hour", "allowed", 0.3, NOW + 1, NOW - 100), NOW)
    assert not quota.stale(R("seven_day", "allowed", 0.3, None, NOW - 6 * 86400), NOW)
    assert quota.stale(R("seven_day", "allowed", 0.3, None, NOW - 8 * 86400), NOW)


def test_current_keeps_only_readings_that_still_speak_for_now():
    fresh = R("five_hour", "allowed", 0.3, NOW + 60, NOW)
    gone = R("seven_day", "allowed", 0.9, NOW - 60, NOW - 100)
    assert quota.current((fresh, gone), NOW) == (fresh,)
    assert quota.current((), NOW) == ()


def test_share_never_makes_a_login_look_like_it_has_room():
    assert quota.share(R("w", "rejected", 0.1, None, NOW)) == 1.0
    assert quota.share(R("w", "allowed_warning", 0.25, None, NOW)) == 0.25  # a figure wins over the warning
    assert quota.share(R("w", "allowed_warning", None, None, NOW)) == 0.8
    assert quota.share(R("w", "allowed", None, None, NOW)) is None  # unknown, not 0% (Coho said 0)
    assert quota.share(R("w", "allowed", 0.4, None, NOW)) == 0.4


# --- pace --------------------------------------------------------------------------------------------------------


def test_window_hours_come_from_the_name_where_it_says():
    assert windows.window_hours("five_hour") == 5
    assert windows.window_hours("seven_day") == windows.window_hours("seven_day_opus") == 168
    assert windows.window_hours("one_day") == 24
    assert windows.window_hours("90_min") == 1.5
    assert windows.window_hours("quota:flash") is None and windows.window_hours("0_min") is None


def test_pace_is_share_used_over_share_of_time_gone():
    six_pm, four_pm = 18 * 3600.0, 16 * 3600.0  # a 5-hour window resetting at 6 pm, read at 4 pm
    assert quota.pace(R("five_hour", "allowed", 0.30, six_pm, four_pm), four_pm) == pytest.approx(0.5)
    assert quota.pace(R("five_hour", "allowed", 0.90, six_pm, four_pm), four_pm) == pytest.approx(1.5)


def test_pace_is_unknown_without_a_length_a_reset_or_a_share():
    assert quota.pace(R("quota:flash", "allowed", 0.5, NOW + 60, NOW), NOW) is None
    assert quota.pace(R("five_hour", "allowed", 0.5, None, NOW), NOW) is None
    assert quota.pace(R("five_hour", "allowed", None, NOW + 60, NOW), NOW) is None
    assert quota.pace(R("five_hour", "allowed", 0.5, NOW - 1, NOW - 60), NOW) is None  # stale: unknown


def test_pace_at_the_very_start_of_a_window_is_bounded():
    start = R("five_hour", "allowed", 0.02, NOW + 5 * 3600, NOW)
    assert quota.pace(start, NOW) == pytest.approx(0.02 / quota.MIN_ELAPSED)
    longer = R("five_hour", "allowed", 0.5, NOW + 9 * 3600, NOW)  # a reset further off than the window is long
    assert quota.pace(longer, NOW) == pytest.approx(0.5 / quota.MIN_ELAPSED)
