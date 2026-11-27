"""How much of a plan's quota a login has used, and how fast (a port of Coho's window gauge).

Every function here is pure: it takes readings (`windows.Reading`) and a time, never the database or a clock. An
unknown is `None`, never 0%: a reading that is past its reset, or undated and a week old, says nothing about now,
and a window with no figure has no share. Nothing here may make a login look like it has more room than it does.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from bosun.providers.catalog import CATALOG
from bosun.providers.windows import MAX_RESET_S, Reading, reset_horizon, valid_reset, window_hours

UNDATED_STALE_S = 7 * 86400  # an undated reading says nothing about now after a week
REJECTED_UNDATED_S = 3600  # ...but a refusal with no usable reset holds the login off an hour, as a limit naming none
WARNING_SHARE = 0.8  # a warning with no figure counts as at least this much used
MIN_ELAPSED = 0.05  # the first 15 minutes of a 5-hour window: pace there is bounded, not share / almost nothing


def _reset(r: Reading) -> float | None:
    return valid_reset(r.window, r.resets_at, r.at)


def stale(r: Reading, now: float) -> bool:
    """Whether a reading says nothing about now: its reset has come, or (with no valid reset) it is a week old, or
    an hour old for a refusal (so a refusal whose reset was dropped still holds the login off, then is asked again)."""
    resets = _reset(r)
    if resets is not None:
        return resets <= now
    if r.status == "rejected":
        return now - r.at > REJECTED_UNDATED_S
    if r.resets_at is not None and r.resets_at <= r.at:
        return True  # a reset before the reading itself: its figure can't be trusted to say anything about now
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

    When the share is known but the window's length or reset is not, treats the window as fully elapsed so
    `pace == share` (a lower bound, never "room"). Unknown (None) when the reading is stale or has no share.
    """
    used, hours = share(r), window_hours(r.window)
    if stale(r, now) or used is None:
        return None
    resets = _reset(r)
    if hours is None or resets is None:
        return used
    elapsed = 1 - (resets - now) / (hours * 3600)
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
            best = Hit(r.window, used, _reset(r))
    return best


def stop_at(bucket: str, overrides: Iterable[Override], now: float) -> float:
    """The share at which background work stops on this login: the line, or 1.0 while its override lasts. An
    override ending further out than any window could reset (a forged or corrupt `until`) lifts nothing."""
    lifted = any(o.bucket == bucket and now < o.until <= now + MAX_RESET_S for o in overrides)
    return 1.0 if lifted else LINE


def override_until(hit: Hit, now: float) -> float:
    """When an override set on this hit ends: the window's reset (bounded by its horizon), or a day when undated."""
    if hit.resets_at is not None and hit.resets_at > now:
        return min(hit.resets_at, now + reset_horizon(hit.window))
    return now + OVERRIDE_UNDATED_S


# --- a call's draw -----------------------------------------------------------------------------------------------

SAME_RESET_S = 60  # resets this close apart are the same window; further apart, it reset in between
DRAW_DROP_EPS = 1e-4  # a fall larger than float noise means a reset or inconsistent reading, not 0 draw


def draw(before: Mapping[str, Reading], after: Iterable[Reading]) -> float | None:
    """The share of a window one call used: the largest rise in any window that kept its reset time (Coho's
    `windowDraw`). None when no window can say. The rise includes other apps' use of the login in between."""
    rises: list[float] = []
    for r in after:
        prev = before.get(r.window)
        if prev is None or prev.used is None or r.used is None:
            continue
        if prev.resets_at is not None and r.resets_at is not None and abs(prev.resets_at - r.resets_at) > SAME_RESET_S:
            continue
        delta = r.used - prev.used
        if delta < -DRAW_DROP_EPS:
            continue
        rises.append(max(0.0, delta))
    return max(rises) if rises else None


# --- where a login's figures come from ---------------------------------------------------------------------------

# Read from output no CLI here has recorded yet (hand-made fixtures, risk R3): its figures count as estimated until a
# recording confirms the shape, then it leaves this set.
UNCONFIRMED: frozenset[str] = frozenset({"codex"})
PROBE_EVERY_S = 15 * 60  # Antigravity's /usage is a call of its own: at most this often


def estimated(provider: str) -> bool:
    """Whether a login's use is counted from calls and tokens rather than read from the plan's own windows."""
    found = CATALOG.get(provider)
    return found is None or found.windows == "estimated" or provider in UNCONFIRMED


def probe_due(last: float | None, now: float) -> bool:
    """Whether a probe (Antigravity's /usage) may run: never tried, 15 minutes since the last try, or a last try
    in the future (the clock went back, so its time says nothing)."""
    return last is None or not 0 <= now - last < PROBE_EVERY_S
