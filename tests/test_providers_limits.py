"""Usage-limit replies take a login out until the reset they name; a 429 cools it down."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from bosun.providers import limits, windows

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC (02:00 in America/Chicago)
HOUR = 3600


def _limit(resets_at: float, at: float = NOW) -> windows.Reading:
    return windows.Reading("limit", "rejected", None, resets_at, at, "limit")


@pytest.mark.parametrize("text,reset", [
    ("You've hit your usage limit. Resets in 4h18m58s", NOW + 4 * HOUR + 18 * 60 + 58),
    ("Resets in 4h18m58s", NOW + 4 * HOUR + 18 * 60 + 58),
    ("Claude usage limit reached; resets 3:50pm", NOW + 7 * HOUR + 50 * 60),  # read in the zone given (UTC)
    ("API Error: Rate limit reached", NOW + HOUR),  # exit 0, still a limit; no reset named: an hour
    ("Claude API rate limit reached. Try again later.", NOW + HOUR),  # recorded 2026-11-26; no reset named: an hour
    # Antigravity, recorded 2026-11-26
    ("Third-party quota reached. Please switch to Gemini models or upgrade your subscription to increase your "
     "limits. Resets in 2h55m54s.", NOW + 2 * HOUR + 55 * 60 + 54),
    # Claude Code, recorded 2026-11-26: the zone it names wins over the machine's
    ("You've hit your 5-hour limit · resets 3:10pm (America/Chicago)", NOW + 13 * HOUR + 10 * 60),
    ("You've hit your 5-hour limit · resets 9:20pm (America/Chicago)", NOW + 19 * HOUR + 20 * 60),
    ("Usage limit reached. Try again in 2 days 3 hours", NOW + 51 * HOUR),
    ("Usage limit reached. Resets in 1 hour and 30 minutes", NOW + HOUR + 30 * 60),
    ("5-hour limit reached, resets 2027-01-15T12:00:00Z", NOW + 4 * HOUR),
    ("2027-01-15T07:59:00Z ERROR: 5-hour limit reached, resets 2027-01-15T12:00:00Z", NOW + 4 * HOUR),
    (f"usage limit reached; reset_at={int(NOW) + 7200}", NOW + 7200),
    ("usage limit reached until 10 minutes ago; resets 3:50pm", NOW + 7 * HOUR + 50 * 60),
    ("You've hit your weekly limit · resets 7am (UTC)", NOW + 23 * HOUR),  # already past today: tomorrow
])
def test_a_limit_reply_is_a_rejected_reading_not_an_answer(text, reset):
    assert limits.limit_reading(text, NOW, tz=UTC) == _limit(reset)


def test_a_named_time_zone_rolls_to_tomorrow_when_that_time_has_passed():
    chicago = ZoneInfo("America/Chicago")
    late = datetime(2026, 11, 26, 22, 0, tzinfo=chicago).timestamp()  # 10:00pm CST: both 3:10pm and 9:20pm passed
    expect_310 = datetime(2026, 11, 27, 15, 10, tzinfo=chicago).timestamp()
    expect_920 = datetime(2026, 11, 27, 21, 20, tzinfo=chicago).timestamp()
    assert limits.limit_reading(
        "You've hit your 5-hour limit · resets 3:10pm (America/Chicago)", late, tz=UTC
    ) == _limit(expect_310, late)
    assert limits.limit_reading(
        "You've hit your 5-hour limit · resets 9:20pm (America/Chicago)", late, tz=UTC
    ) == _limit(expect_920, late)


def test_a_named_date_is_read_as_that_date():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).timestamp()
    assert limits.limit_reading("try again at Oct 24th, 2026 3:05 PM", now, tz=UTC) == _limit(
        now + 3 * HOUR + 5 * 60, now)
    got = limits.limit_reading("Usage limit reached, try again at Oct 24th, 2026 3:05 PM", now, tz=UTC)
    assert got == _limit(now + 3 * HOUR + 5 * 60, now)
    # a named date already past says nothing about when: an hour
    late = limits.limit_reading("Usage limit reached, try again at Oct 24th, 2026 3:05 PM", now + 9 * HOUR, tz=UTC)
    assert late == _limit(now + 10 * HOUR, now + 9 * HOUR)
    # a month/day with no year already past rolls to next year
    next_day = datetime(2026, 10, 25, 12, 0, tzinfo=UTC).timestamp()
    expect_next_year = datetime(2027, 10, 24, 15, 5, tzinfo=UTC).timestamp()
    assert limits.limit_reading("Usage limit reached, resets Oct 24, 3:05 PM", next_day, tz=UTC) == _limit(
        expect_next_year, next_day)


def test_a_clock_time_is_read_in_the_machines_zone_by_default_and_a_bad_zone_falls_back():
    chicago = ZoneInfo("America/Chicago")
    expect = datetime(2027, 1, 15, 15, 10, tzinfo=chicago).timestamp()
    assert limits.limit_reading("5-hour limit · resets 3:10pm", NOW, tz=chicago) == _limit(expect)
    assert limits.limit_reading("5-hour limit · resets 3:10pm (Mars/Olympus)", NOW, tz=chicago) == _limit(expect)
    local = datetime.fromtimestamp(NOW).replace(hour=15, minute=10, second=0, microsecond=0)
    local_reset = local.timestamp() if local.timestamp() > NOW else local.timestamp() + 24 * HOUR
    assert limits.limit_reading("5-hour limit · resets 3:10pm", NOW) == _limit(local_reset)


def test_an_answer_that_mentions_limits_is_still_an_answer():
    assert limits.limit_reading('{"headline": "Raises the API rate limit"}', NOW) is None
    assert limits.limit_reading("Resets the cache at 3pm and runs out of memory less", NOW) is None
    assert limits.limit_reading("", NOW) is None


def test_a_reset_bosun_cannot_read_means_an_hour():
    assert limits.limit_reading("usage limit reached, resets 2027-99-99T00:00:00Z", NOW, tz=UTC) == _limit(NOW + HOUR)
    assert limits.limit_reading("usage limit reached, resets 25:99pm", NOW, tz=UTC) == _limit(NOW + HOUR)
    assert limits.limit_reading("usage limit reached, resets 13pm", NOW, tz=UTC) == _limit(NOW + HOUR)
    assert limits.limit_reading("usage limit reached, resets 0am", NOW, tz=UTC) == _limit(NOW + HOUR)
    assert limits.limit_reading("usage limit reached, resets in 0m", NOW, tz=UTC) == _limit(NOW + HOUR)
    assert limits.limit_reading("usage limit reached, try again at Feb 30, 2027 1pm", NOW, tz=UTC) == _limit(
        NOW + HOUR)
    leap_past = datetime(2028, 3, 1, 12, 0, tzinfo=UTC).timestamp()
    assert limits.limit_reading("usage limit reached, resets Feb 29, 1pm", leap_past, tz=UTC) == _limit(
        leap_past + HOUR, leap_past)


# --- 429 cool-downs ----------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_a_rate_limit_cools_the_login_down_30s_doubling():
    clock = Clock(NOW)
    cool = limits.Cooldowns(clock=clock)
    assert cool.until("codex") is None and cool.active() == frozenset()
    assert cool.hit("codex") == NOW + 30 and cool.until("codex") == NOW + 30
    assert cool.active() == frozenset({"codex"})
    assert cool.hit("codex") == NOW + 60
    assert cool.until("claude") is None  # per login
    clock.t = NOW + 61
    assert cool.until("codex") is None and cool.active() == frozenset()  # over by itself
    assert cool.hit("codex") == NOW + 61 + 120  # still doubling: no success came in between
    cool.clear("codex")  # a success resets the step
    assert cool.until("codex") is None and cool.hit("codex") == NOW + 61 + 30


def test_a_cool_down_is_capped_at_15_minutes():
    cool = limits.Cooldowns(clock=Clock(NOW))
    for _ in range(10):
        last = cool.hit("grok@work")
    assert last == NOW + limits.COOLDOWN_MAX_S == NOW + 900
    cool.clear("nobody")  # clearing a login that never cooled is fine
