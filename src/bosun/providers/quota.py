"""How much of a plan's quota a login has used, and how fast (a port of Coho's window gauge).

Every function here is pure: it takes readings (`windows.Reading`) and a time, never the database or a clock. An
unknown is `None`, never 0%: a reading that is past its reset, or undated and a week old, says nothing about now,
and a window with no figure has no share. Nothing here may make a login look like it has more room than it does.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

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


def login_pace(readings: Iterable[Reading], now: float) -> float | None:
    """A login is judged by its worst current window. A refused window is `inf`: balancing by pace never picks it.

    None when no current window has a pace: the router ranks such a login in the middle, so it gets a turn.
    """
    now_readings = current(readings, now)
    if any(r.status == "rejected" for r in now_readings):
        return math.inf
    paces = [p for p in (pace(r, now) for r in now_readings) if p is not None]
    return max(paces) if paces else None


# --- the 75% line ------------------------------------------------------------------------------------------------

LINE = 0.75  # background jobs keep off a login with any current window at or past this share
OVERRIDE_UNDATED_S = 86400  # an override on a window with no reset time ends after a day


@dataclass(frozen=True)
class Hit:
    """The window that put a login at or past the line."""

    window: str
    used: float
    resets_at: float | None


@dataclass(frozen=True)
class Override:
    """The owner's "let background work go past 75% until it resets", for one login."""

    bucket: str
    until: float


def line_hit(readings: Iterable[Reading], now: float, line: float = LINE) -> Hit | None:
    """The most-used current window at or past `line`; a refused window always counts. None when none is (or
    every share is unknown: an unknown share has nothing to apply the line to)."""
    best: Hit | None = None
    for r in current(readings, now):
        used = share(r)
        if used is None or (used < line and r.status != "rejected"):
            continue
        if best is None or used > best.used:
            best = Hit(r.window, used, r.resets_at)
    return best


def stop_at(bucket: str, overrides: Iterable[Override], now: float) -> float:
    """The share at which background work stops on this login: the line, or 1.0 while its override lasts."""
    lifted = any(o.bucket == bucket and o.until > now for o in overrides)
    return 1.0 if lifted else LINE


def override_until(hit: Hit, now: float) -> float:
    """When an override set on this hit ends: the window's reset, or a day from now when it has none (or it passed)."""
    if hit.resets_at is not None and hit.resets_at > now:
        return hit.resets_at
    return now + OVERRIDE_UNDATED_S
