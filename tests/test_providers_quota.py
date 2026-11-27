"""Staleness, share, pace, the 75% line and each call's draw on a usage window."""

import math

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


def test_pace_is_unknown_without_a_reset_or_a_share_and_uses_share_without_length():
    assert quota.pace(R("quota:flash", "allowed", 0.5, NOW + 60, NOW), NOW) == pytest.approx(0.5)
    assert quota.pace(R("quota:flash", "allowed", 0.99, None, NOW), NOW) == pytest.approx(0.99)
    assert quota.pace(R("quota:flash", "allowed", None, NOW + 60, NOW), NOW) is None
    assert quota.pace(R("five_hour", "allowed", 0.5, None, NOW), NOW) is None
    assert quota.pace(R("five_hour", "allowed", None, NOW + 60, NOW), NOW) is None
    assert quota.pace(R("five_hour", "allowed", 0.5, NOW - 1, NOW - 60), NOW) is None  # stale: unknown


def test_pace_at_the_very_start_of_a_window_is_bounded():
    start = R("five_hour", "allowed", 0.02, NOW + 5 * 3600, NOW)
    assert quota.pace(start, NOW) == pytest.approx(0.02 / quota.MIN_ELAPSED)
    longer = R("five_hour", "allowed", 0.5, NOW + 5.5 * 3600, NOW)  # slightly past nominal length (within slack)
    assert quota.pace(longer, NOW) == pytest.approx(0.5 / quota.MIN_ELAPSED)


# --- a login -----------------------------------------------------------------------------------------------------


def _at_pace(window: str, p: float) -> windows.Reading:
    """A reading halfway through its window (elapsed 0.5), so its pace is twice its share."""
    hours = windows.window_hours(window) or 5
    return R(window, "allowed", p / 2, NOW + hours * 1800, NOW)


def test_login_pace_is_the_worst_current_window_and_none_without_one():
    gone = R("seven_day_opus", "allowed", 0.99, NOW - 1, NOW - 9000)
    readings = (_at_pace("five_hour", 0.4), _at_pace("seven_day", 1.2), gone)
    assert quota.login_pace(readings, NOW) == pytest.approx(1.2)
    assert quota.login_pace((), NOW) is None
    assert quota.login_pace((gone,), NOW) is None
    refused = R("five_hour", "rejected", None, NOW + 60, NOW)
    assert quota.login_pace((_at_pace("seven_day", 0.1), refused), NOW) == math.inf
    unknown_len = R("quota:flash", "allowed", 0.99, NOW + 60, NOW)  # no length: pace equals share (fully elapsed)
    assert quota.login_pace((unknown_len,), NOW) == pytest.approx(0.99)
    no_share = R("quota:flash", "allowed", None, NOW + 60, NOW)
    assert quota.login_pace((no_share,), NOW) is None


# --- the 75% line ------------------------------------------------------------------------------------------------


def test_the_line_names_the_most_used_window_at_or_past_it():
    rs = (R("five_hour", "allowed", 0.74, NOW + 60, NOW), R("seven_day", "allowed", 0.81, NOW + 9000, NOW))
    assert quota.line_hit(rs, NOW, 0.75) == quota.Hit("seven_day", 0.81, NOW + 9000)
    assert quota.line_hit(rs[:1], NOW, 0.75) is None
    assert quota.line_hit((R("w", "allowed", None, None, NOW),), NOW, 0.75) is None  # unknown: nothing to apply
    assert quota.line_hit((R("five_hour", "allowed", 0.9, NOW - 1, NOW - 60),), NOW, 0.75) is None  # stale
    refused = R("five_hour", "rejected", None, NOW + 60, NOW)
    assert quota.line_hit((refused,), NOW, quota.LINE) == quota.Hit("five_hour", 1.0, NOW + 60)
    assert quota.line_hit((refused,), NOW, 1.0) == quota.Hit("five_hour", 1.0, NOW + 60)  # a refusal is always out


def test_the_override_lifts_the_line_for_one_login_until_its_reset():
    o = quota.Override(bucket="claude", until=NOW + 60)
    assert quota.stop_at("claude", (o,), NOW) == 1.0
    assert quota.stop_at("codex", (o,), NOW) == quota.LINE == 0.75
    assert quota.stop_at("claude", (o,), NOW + 61) == 0.75  # ended at the reset by itself
    assert quota.stop_at("claude", (), NOW) == 0.75


def test_an_override_ends_at_the_hit_windows_reset_or_in_a_day_when_undated():
    assert quota.override_until(quota.Hit("seven_day", 0.8, NOW + 9000), NOW) == NOW + 9000
    assert quota.override_until(quota.Hit("quota:flash", 0.8, None), NOW) == NOW + 86400
    assert quota.override_until(quota.Hit("five_hour", 0.8, NOW - 5), NOW) == NOW + 86400  # past: a day, not none


def test_far_future_reset_goes_stale_and_override_is_bounded_to_window_horizon():
    far = R("five_hour", "allowed", 0.9, NOW + 365 * 86400, NOW)
    assert not quota.stale(far, NOW)
    assert quota.stale(far, NOW + 8 * 86400)  # dropped far-future reset falls back to undated 7-day staleness
    assert quota.pace(far, NOW) is None
    assert quota.line_hit((far,), NOW) == quota.Hit("five_hour", 0.9, None)
    assert quota.override_until(quota.Hit("five_hour", 0.8, NOW + 365 * 86400), NOW) == (
        NOW + 5 * 3600 + windows.RESET_SLACK_S)
    assert quota.override_until(quota.Hit("quota:flash", 0.8, NOW + 365 * 86400), NOW) == NOW + windows.MAX_RESET_S


# --- a call's draw -----------------------------------------------------------------------------------------------


def test_draw_is_the_largest_rise_in_a_window_that_kept_its_reset():
    r = NOW + 3600
    before = {"five_hour": R("five_hour", "allowed", 0.30, r, NOW), "seven_day": R("seven_day", "allowed", 0.5, None, NOW)}
    assert quota.draw(before, (R("five_hour", "allowed", 0.34, r + 30, NOW + 9),)) == pytest.approx(0.04)
    both = (R("five_hour", "allowed", 0.34, r, NOW + 9), R("seven_day", "allowed", 0.51, None, NOW + 9))
    assert quota.draw(before, both) == pytest.approx(0.04)
    assert quota.draw(before, (R("five_hour", "allowed", 0.05, r + 18000, NOW + 9),)) is None  # it reset
    assert quota.draw({}, (R("five_hour", "allowed", 0.34, r, NOW + 9),)) is None
    assert quota.draw(before, (R("five_hour", "allowed", 0.20, r, NOW + 9),)) is None  # a drop beyond epsilon is unknown
    assert quota.draw(before, (R("seven_day", "allowed", 0.10, None, NOW + 9),)) is None  # drop with unknown reset
    assert quota.draw(before, (R("five_hour", "allowed", 0.30 - 1e-6, r, NOW + 9),)) == 0.0  # tiny float noise is 0
    assert quota.draw(before, (R("five_hour", "allowed", 0.20, r, NOW + 9),
                               R("seven_day", "allowed", 0.52, None, NOW + 9))) == pytest.approx(0.02)
    assert quota.draw(before, (R("five_hour", "rejected", None, r, NOW + 9),)) is None  # no figure to compare


# --- where a login's figures come from ---------------------------------------------------------------------------


def test_codex_and_grok_count_as_estimated_until_a_real_reading_is_recorded():
    assert {p: quota.estimated(p) for p in ("claude", "codex", "gemini", "grok", "antigravity", "muse")} == {
        "claude": False, "codex": True, "gemini": True, "grok": True, "antigravity": False, "muse": True}
    assert quota.estimated("nonesuch")  # a CLI Bosun has no spec for has no readings Bosun trusts


def test_antigravitys_usage_is_read_at_most_every_15_minutes():
    assert quota.probe_due(None, NOW)
    assert not quota.probe_due(NOW - 14 * 60, NOW)
    assert quota.probe_due(NOW - 15 * 60, NOW)
    assert quota.probe_due(NOW + 3600, NOW)  # a clock that went back: the last probe's time says nothing
