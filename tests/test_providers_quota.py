"""Staleness, share, pace, the 75% line and each call's draw on a usage window."""

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
