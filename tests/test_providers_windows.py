"""Usage windows as each CLI reports them: Claude's rate_limit_event, Codex's rate_limits, Antigravity's /usage."""

import json
from pathlib import Path

from bosun.providers import windows

FIX = Path(__file__).parent / "fixtures" / "cli"
R = windows.Reading


def _event(**info) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


# --- Claude ------------------------------------------------------------------------------------------------------


def test_claudes_recorded_stream_gives_the_weekly_window():
    raw = (FIX / "claude/2.1.318/stream-json.jsonl").read_text()
    assert windows.parse_claude(raw, at=100.0) == (R("seven_day", "allowed", 0.58, 1794312000.0, 100.0),)


def test_unified_windows_each_become_a_reading_and_a_share_over_one_is_capped():
    line = _event(status="allowed_warning", rateLimitType="five_hour", resetsAt=1800000000, unifiedWindows={
        "five_hour": {"utilization": 1.4, "resetsAt": 1800000000},
        "seven_day": {"utilization": 0.2, "resetsAt": 1800500000, "status": "allowed"}})
    got = {r.window: r for r in windows.parse_claude(line + "\nnot json\n", at=1.0)}
    assert got["five_hour"].used == 1.0 and got["five_hour"].status == "allowed_warning"
    assert got["seven_day"].used == 0.2 and got["seven_day"].resets_at == 1800500000.0
    assert len(got) == 2


def test_an_unknown_window_name_is_kept_under_its_own_name():
    got = windows.parse_claude(_event(status="allowed", rateLimitType="seven_day_fable", utilization=41), at=1.0)
    assert got == (R("seven_day_fable", "allowed", 0.41, None, 1.0),)


def test_a_bad_window_name_or_status_is_dropped():
    assert windows.parse_claude(_event(status="maybe", rateLimitType="five_hour"), at=1.0) == ()
    assert windows.parse_claude(_event(status="allowed", rateLimitType="../x"), at=1.0) == (
        R("window", "allowed", None, None, 1.0),)


def test_claude_times_in_milliseconds_or_iso_and_junk_are_read_or_dropped():
    ms = _event(status="rejected", rateLimitType="five_hour", resetsAt=1800000000000)
    iso = _event(status="allowed", rateLimitType="seven_day", resetsAt="2027-01-01T00:00:00Z")
    junk = _event(status="allowed", rateLimitType="seven_day", resetsAt="soon", utilization=-3)
    assert windows.parse_claude(ms, at=1.0)[0].resets_at == 1800000000.0
    assert windows.parse_claude(iso, at=1.0)[0].resets_at == 1798761600.0
    assert windows.parse_claude(junk, at=1.0) == (R("seven_day", "allowed", None, None, 1.0),)


def test_claude_lines_that_are_not_events_are_ignored():
    raw = "\n".join([
        json.dumps({"type": "result", "result": "rate_limit_event"}),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": "nope"}),
        json.dumps(["rate_limit_event"]),
        "{rate_limit_event",
    ])
    assert windows.parse_claude(raw, at=1.0) == ()
    assert windows.parse_claude("", at=1.0) == ()
