"""How much of a plan's quota a login has used, and how fast (a port of Coho's window gauge).

Every function here is pure: it takes readings (`windows.Reading`) and a time, never the database or a clock. An
unknown is `None`, never 0%: a reading that is past its reset, or undated and a week old, says nothing about now,
and a window with no figure has no share. Nothing here may make a login look like it has more room than it does.
"""

from __future__ import annotations

from collections.abc import Iterable

from bosun.providers.windows import Reading, window_hours

UNDATED_STALE_S = 7 * 86400  # an undated reading says nothing about now after a week
WARNING_SHARE = 0.8  # a warning with no figure counts as at least this much used
MIN_ELAPSED = 0.05  # the first 15 minutes of a 5-hour window: pace there is bounded, not share / almost nothing


def stale(r: Reading, now: float) -> bool:
    """Whether a reading says nothing about now: its reset has come, or (with no reset) it is a week old."""
    if r.resets_at is not None:
        return r.resets_at <= now
    return now - r.at > UNDATED_STALE_S


def current(readings: Iterable[Reading], now: float) -> tuple[Reading, ...]:
    """The readings that still speak for now."""
    return tuple(r for r in readings if not stale(r, now))


def share(r: Reading) -> float | None:
    """A window's share used, 0-1: a refusal is 1; else the CLI's figure; a bare warning is 0.8; else unknown.

    Claude warns well below 80% (seven_day `allowed_warning` at 25%), so a figure wins over the warning.
    """
    if r.status == "rejected":
        return 1.0
    if r.used is not None:
        return r.used
    return WARNING_SHARE if r.status == "allowed_warning" else None


def pace(r: Reading, now: float) -> float | None:
    """Share used over the share of the window's time gone: 1.0 is on track to use it all exactly at the reset.

    Unknown (None) when the reading is stale, has no share, no reset, or a window whose length its name does not say.
    """
    used, hours = share(r), window_hours(r.window)
    if stale(r, now) or used is None or r.resets_at is None or hours is None:
        return None
    elapsed = 1 - (r.resets_at - now) / (hours * 3600)
    return used / min(1.0, max(MIN_ELAPSED, elapsed))
