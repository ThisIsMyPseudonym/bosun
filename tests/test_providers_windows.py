"""Usage windows as each CLI reports them: Claude's rate_limit_event, Codex's rate_limits, Antigravity's /usage."""

import json
from pathlib import Path

import pytest

from bosun.providers import windows

FIX = Path(__file__).parent / "fixtures" / "cli"
R = windows.Reading


def _event(**info) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


# --- Claude ------------------------------------------------------------------------------------------------------


def test_claudes_recorded_stream_gives_the_weekly_window():
    raw = (FIX / "claude/2.1.318/stream-json.jsonl").read_text()
    at = 1794312000.0 - 3600
    assert windows.parse_claude(raw, at=at) == (R("seven_day", "allowed", 0.58, 1794312000.0, at),)


def test_unified_windows_each_become_a_reading_and_a_share_over_one_is_capped():
    at = 1799990000.0
    line = _event(status="allowed_warning", rateLimitType="five_hour", resetsAt=1800000000, unifiedWindows={
        "five_hour": {"utilization": 1.4, "resetsAt": 1800000000},
        "seven_day": {"utilization": 0.2, "resetsAt": 1800500000, "status": "allowed"}})
    got = {r.window: r for r in windows.parse_claude(line + "\nnot json\n", at=at)}
    assert got["five_hour"].used == 1.0 and got["five_hour"].status == "allowed_warning"
    assert got["seven_day"].used == 0.2 and got["seven_day"].resets_at == 1800500000.0
    assert len(got) == 2


def test_an_unknown_window_name_is_kept_under_its_own_name():
    got = windows.parse_claude(_event(status="allowed", rateLimitType="seven_day_fable", utilization=0.41), at=1.0)
    assert got == (R("seven_day_fable", "allowed", 0.41, None, 1.0),)


def test_claude_top_level_utilization_over_one_is_capped_at_one():
    got = windows.parse_claude(_event(status="rejected", rateLimitType="five_hour", utilization=1.5), at=1.0)
    assert got == (R("five_hour", "rejected", 1.0, None, 1.0),)


def test_a_bad_window_name_or_status_is_dropped():
    assert windows.parse_claude(_event(status="maybe", rateLimitType="five_hour"), at=1.0) == ()
    assert windows.parse_claude(_event(status="allowed", rateLimitType="../x"), at=1.0) == (
        R("window", "allowed", None, None, 1.0),)


def test_claude_times_in_milliseconds_or_iso_and_junk_are_read_or_dropped():
    ms = _event(status="rejected", rateLimitType="five_hour", resetsAt=1800000000000)
    iso = _event(status="allowed", rateLimitType="seven_day", resetsAt="2027-01-01T00:00:00Z")
    junk = _event(status="allowed", rateLimitType="seven_day", resetsAt="soon", utilization=-3)
    assert windows.parse_claude(ms, at=1799990000.0)[0].resets_at == 1800000000.0
    assert windows.parse_claude(iso, at=1798750000.0)[0].resets_at == 1798761600.0
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


# --- Codex -------------------------------------------------------------------------------------------------------


def test_codex_primary_is_five_hours_and_secondary_weekly():
    at = 1799990000.0
    line = json.dumps({"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 42.0, "window_minutes": 300, "resets_at": 1800000000},
        "secondary": {"used_percent": 100, "window_minutes": 10080, "resets_in_seconds": 3600}}})
    got = {r.window: r for r in windows.parse_codex(line, at=at)}
    assert got["five_hour"].used == 0.42 and got["five_hour"].status == "allowed"
    assert got["seven_day"].status == "rejected" and got["seven_day"].resets_at == at + 3600.0


def test_codex_camel_case_app_server_shape_is_read_too():
    at = 1798750000.0
    reply = json.dumps({"id": 3, "result": {"rateLimits": {
        "primary": {"usedPercent": 91, "windowDurationMins": 1440, "resetsAt": "2027-01-01T00:00:00Z"},
        "secondary": {"usedPercent": 5, "windowDurationMins": 90}}}})
    note = json.dumps({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1798760000}}}})
    got = {r.window: r for r in windows.parse_codex(reply + "\n" + note, at=at)}
    assert got["one_day"] == R("one_day", "allowed_warning", 0.91, 1798761600.0, at)
    assert got["90_min"].used == 0.05 and got["90_min"].resets_at is None
    assert got["five_hour"].used == 0.1


def test_codex_recorded_shape_and_junk():
    raw = (FIX / "codex/handmade/exec-json.jsonl").read_text()
    assert {r.window: r.used for r in windows.parse_codex(raw, at=1.0)} == {"five_hour": 0.12, "seven_day": 0.315}
    junk = json.dumps({"rate_limits": {"primary": {"used_percent": "lots"}, "secondary": None}})
    nameless = json.dumps({"rate_limits": {"primary": {"used_percent": 3, "window_minutes": -1}}})
    assert windows.parse_codex(junk + "\nrate_limits {\n", at=1.0) == ()
    assert windows.parse_codex(nameless, at=1.0) == (R("primary", "allowed", 0.03, None, 1.0),)
    assert windows.parse_codex(json.dumps({"rate_limits": []}), at=1.0) == ()


def test_unnamed_codex_slots_stay_distinct_and_colliding_windows_keep_worst():
    unnamed = json.dumps({"rate_limits": {"primary": {"used_percent": 95}, "secondary": {"used_percent": 10}}})
    got = {r.window: r for r in windows.parse_codex(unnamed, at=1.0)}
    assert got == {
        "primary": R("primary", "allowed_warning", 0.95, None, 1.0),
        "secondary": R("secondary", "allowed", 0.10, None, 1.0),
    }
    same_len = json.dumps({"rate_limits": {
        "primary": {"used_percent": 95, "window_minutes": 300},
        "secondary": {"used_percent": 10, "window_minutes": 300}}})
    assert windows.parse_codex(same_len, at=1.0) == (R("five_hour", "allowed_warning", 0.95, None, 1.0),)
    prefix = "super-long-model-name-that-truncates-at-40"
    agy = json.dumps([
        {"model": prefix + "-alpha", "quotaInfo": {"remainingFraction": 0.0}},
        {"model": prefix + "-beta", "quotaInfo": {"remainingFraction": 0.9}},
    ])
    (collided,) = windows.parse_antigravity(agy, at=1.0)
    assert collided.status == "rejected" and collided.used == 1.0
    claude_dup = "\n".join([
        _event(status="rejected", rateLimitType="five_hour", utilization=0.95),
        _event(status="allowed", rateLimitType="five_hour", utilization=0.10),
    ])
    assert windows.parse_claude(claude_dup, at=1.0) == (R("five_hour", "rejected", 0.95, None, 1.0),)


# --- Antigravity -------------------------------------------------------------------------------------------------


def test_antigravity_usage_json_and_tab_lines_give_remaining_as_used():
    at = 1795640000.0
    js = json.dumps([{"model": "Gemini 3.8 Flash", "quotaInfo": {"remainingFraction": 0.25,
                                                                 "resetTime": "2026-11-26T00:00:00Z"}}])
    (r,) = windows.parse_antigravity(js, at=at)
    assert r == R("quota:gemini-3.8-flash", "allowed", 0.75, 1795651200.0, at, "probe")
    (t,) = windows.parse_antigravity("Model\tRemaining\tResets\nGemini 3.8 Flash\t10%\t2026-11-26T00:00:00Z\n", at=at)
    assert t.window == "quota:gemini-3.8-flash" and t.used == pytest.approx(0.9) and t.status == "allowed_warning"
    assert t.resets_at == 1795651200.0


def test_antigravity_other_shapes_and_junk():
    wrapped = json.dumps({"quotas": [{"label": "Argon", "remainingPercentage": 0},
                                     {"name": "x", "quota_info": {"remaining_fraction": "lots"}}, "junk"]})
    (argon,) = windows.parse_antigravity(wrapped, at=1.0)
    assert argon == R("quota:argon", "rejected", 1.0, None, 1.0, "probe")
    single = json.dumps({"remainingFraction": 0.5})
    assert windows.parse_antigravity(single, at=1.0) == (R("quota", "allowed", 0.5, None, 1.0, "probe"),)
    assert windows.parse_antigravity("{broken\nFlash\t0.4\n", at=1.0)[0].used == pytest.approx(0.6)
    assert windows.parse_antigravity("Third-party quota reached. Resets in 2h55m54s.", at=1.0) == ()
    assert windows.parse_antigravity("a\tb\n\n", at=1.0) == ()


def test_parsers_never_raise_on_deeply_nested_or_oversized_json():
    assert windows.parse_claude("[" * 200_000 + "rate_limit_event", 0.0) == ()
    assert windows.parse_claude("[" * 10_000 + "rate_limit_event", 0.0) == ()
    assert windows.parse_codex("[" * 200_000 + "rate_limits", 0.0) == ()
    assert windows.parse_codex("[" * 10_000 + "rate_limits", 0.0) == ()
    assert windows.parse_antigravity("[" * 200_000, 0.0) == ()
    assert windows.parse_antigravity("[" * 10_000, 0.0) == ()


def test_far_future_reset_beyond_window_horizon_is_dropped():
    at = 1_800_000_000.0
    ok_line = _event(status="allowed", rateLimitType="five_hour", utilization=0.4, resetsAt=at + 6 * 3600)
    far_line = _event(status="allowed", rateLimitType="five_hour", utilization=0.4, resetsAt=at + 6 * 3600 + 1)
    assert windows.parse_claude(ok_line, at=at)[0].resets_at == at + 6 * 3600
    assert windows.parse_claude(far_line, at=at)[0].resets_at is None
    codex_far = json.dumps({"rate_limits": {"primary": {"used_percent": 50, "window_minutes": 300,
                                                        "resets_at": at + 7 * 86400}}})
    assert windows.parse_codex(codex_far, at=at)[0].resets_at is None
    agy_far = json.dumps([{"model": "Flash", "quotaInfo": {"remainingFraction": 0.5,
                                                           "resetTime": "2099-01-01T00:00:00Z"}}])
    assert windows.parse_antigravity(agy_far, at=at)[0].resets_at is None
