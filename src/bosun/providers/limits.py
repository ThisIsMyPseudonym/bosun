"""When a provider refuses a login: usage-limit replies and 429 cool-downs (a port of Coho's `llmProviders.ts`).

A usage-limit reply is not an answer, even with exit code 0 (Claude Code prints "API Error: Rate limit reached" and
exits 0). It becomes a `limit` reading, `rejected`, that resets when the reply says, or in an hour when it names no
time Bosun can read. Only call `limit_reading` on a reply that did not parse as the answer the job expected: the
words it looks for can appear in an answer about rate limits.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bosun.providers.windows import Reading

NO_RESET_S = 3600  # a limit that names no reset keeps the login out for an hour
MAX_RESET_S = 31 * 86400.0  # resets further out than a month are unreadable (fall back to an hour)
MAX_LIMIT_CHARS = 500  # real limit errors are short; longer text is a normal answer quoting limit words

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
_UNIT = r"(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
_SEP = r"(?:[\s,]*(?:and[\s,]+)?)"

# Coho's LIMIT_WORDS, plus Claude Code's "Rate limit reached" (exit 0), bare "Resets in <dur>" / "try again at <time>",
# and with "run out of" narrowed to quota.
LIMIT_WORDS = re.compile(
    r"usage limit|limit will reset|rate limit reached|hit your (?:usage |session |weekly |5-hour |daily )?limit|"
    r"out of (?:usage|credits|quota)|quota (?:exceeded|exhausted|reached)|exceeded your (?:current )?quota|"
    r"insufficient_quota|credits? balance is too low|free (?:tier )?(?:limit|quota)|free allocation|weekly limit|"
    r"5-hour limit|session limit|run out of (?:usage|credits|quota|messages)|"
    r"no (?:more )?(?:messages|requests) (?:left|remaining)|upgrade (?:your plan|to (?:pro|max|plus))|"
    rf"\bresets?\s+in\s+\d+(?:\.\d+)?\s*{_UNIT}|"
    rf"\btry again at\s+(?:{_MONTH}[a-z]*\.?\s+\d|\d{{1,2}}(?::\d\d)?\s*(?:am|pm)|\d{{1,2}}:\d\d)",
    re.I)

_ISO = re.compile(r"\b(20\d\d-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)")
_EPOCH = re.compile(r"\breset(?:s|_at|sAt|At)?[\"'=:\s|]+(1[6-9]\d{8})\b", re.I)
_RELATIVE = re.compile(rf"\b(?:try again|retry|resets?|available again)\s+in\s+((?:\d+(?:\.\d+)?\s*{_UNIT}{_SEP})+)",
                       re.I)
_PART = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT})", re.I)
_CLOCK = re.compile(
    r"\b(?:resets?|reset at|try again at|available again at|until)\s+(?:at\s+)?"
    rf"(?:(?P<mon>{_MONTH})[a-z]*\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>20\d\d),?\s+)?(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d\d)\s*(?P<ampm1>am|pm)?|\s*(?P<ampm2>am|pm))\b"
    r"(?:\s*\(\s*(?P<pzone>[A-Za-z][A-Za-z0-9_/+-]{0,40})\s*\)"
    r"|\s+(?P<zone>UTC|GMT|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+|(?-i:[A-Z]{2,5}))\b)?",
    re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), start=1)}
_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _in_horizon(t: float, now: float) -> bool:
    return math.isfinite(t) and now < t <= now + MAX_RESET_S


def _iso(text: str, now: float) -> list[float]:
    out: list[float] = []
    for m in _ISO.finditer(text):
        try:
            parsed = datetime.fromisoformat(m[1].replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo and _in_horizon(parsed.timestamp(), now):
            out.append(parsed.timestamp())
    return out


def _epoch(text: str, now: float) -> list[float]:
    out: list[float] = []
    for m in _EPOCH.finditer(text):
        t = float(m[1])
        if _in_horizon(t, now):
            out.append(t)
    return out


def _relative(text: str, now: float) -> list[float]:
    out: list[float] = []
    for m in _RELATIVE.finditer(text):
        seconds = sum(float(n) * _SECONDS[unit[0].lower()] for n, unit in _PART.findall(m[1]))
        if math.isfinite(seconds) and 0 < seconds <= MAX_RESET_S:
            out.append(now + seconds)
    return out


def _zone(name: str | None, fallback: tzinfo | None) -> tuple[bool, tzinfo | None]:
    """(ok, zone): the zone a reply names ("(America/Chicago)", "UTC"), `fallback` when unnamed, else (False, None)."""
    if name is None:
        return True, fallback
    if name.upper() in ("UTC", "GMT"):
        return True, UTC
    try:
        return True, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False, None


def _hour(m: re.Match[str]) -> tuple[int, int] | None:
    hour, minute = int(m["hour"]), int(m["min"] or 0)
    ampm = (m["ampm1"] or m["ampm2"] or "").lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour + 12 if ampm == "pm" and hour < 12 else 0 if ampm == "am" and hour == 12 else hour
    return (hour, minute) if hour <= 23 else None


def _clock_match(m: re.Match[str], now: float, tz: tzinfo | None) -> float | None:
    hm = _hour(m)
    ok, zone = _zone(m["pzone"] or m["zone"], tz)
    if hm is None or not ok:
        return None
    today = datetime.fromtimestamp(now, zone)
    try:
        at = today.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if m["mon"]:
            at = at.replace(year=int(m["year"] or at.year), month=_MONTHS[m["mon"].lower()[:3]], day=int(m["day"]))
    except ValueError:  # Feb 30
        return None
    if at.timestamp() > now:
        return at.timestamp() if at.timestamp() <= now + 366 * 86400 else None
    if m["year"]:
        return None  # a named date already past says nothing about when
    wall = at.replace(tzinfo=None)  # step the wall clock, then read it in the zone again (DST moves the offset)
    try:
        wall = wall.replace(year=wall.year + 1) if m["mon"] else wall + timedelta(days=1)
    except ValueError:  # Feb 29 next year
        return None
    return wall.replace(tzinfo=zone).timestamp()


def _clock(text: str, now: float, tz: tzinfo | None) -> list[float]:
    """Clock times ("resets 3:50pm", "try again at Oct 24th, 2026 3:05 PM"), in the zone named or `tz`.
    A time with no date already past means tomorrow; a date with no year already past means next year."""
    found = (_clock_match(m, now, tz) for m in _CLOCK.finditer(text))
    return [t for t in found if t is not None]


def reset_time(text: str, now: float, tz: tzinfo | None = None) -> float | None:
    """When a limit message says the limit resets (epoch seconds), taking the latest plausible candidate."""
    flat = " ".join(text.split())
    candidates = (*_iso(flat, now), *_epoch(flat, now), *_relative(flat, now), *_clock(flat, now, tz))
    return max(candidates) if candidates else None


def limit_reading(text: str, now: float, tz: tzinfo | None = None) -> Reading | None:
    """A usage-limit reply as a `rejected` reading of the `limit` window, or None when the text is not one."""
    trimmed = text.strip()
    if not trimmed or len(trimmed) > MAX_LIMIT_CHARS or not LIMIT_WORDS.search(trimmed):
        return None
    resets = reset_time(trimmed, now, tz)
    return Reading("limit", "rejected", None, resets if resets is not None else now + NO_RESET_S, now, "limit")


# --- 429 cool-downs ----------------------------------------------------------------------------------------------

COOLDOWN_START_S = 30.0  # the first 429 keeps the login out this long; each one after doubles it
COOLDOWN_MAX_S = 15 * 60.0


@dataclass(frozen=True)
class Cooldown:
    until: float
    step: float  # the length of the last cool-down: the next one is twice it


class Cooldowns:
    """Which logins a 429 has cooled down, in memory for this process (the dashboard runs every refresh since
    single-writer refresh routing, so one process sees them all). A success clears a login's step."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state: Mapping[str, Cooldown] = MappingProxyType({})

    def hit(self, bucket: str) -> float:
        """A rate limit on this login: out for 30 s, doubling with each one before a success. Returns until when."""
        with self._lock:
            prev = self._state.get(bucket)
            step = COOLDOWN_START_S if prev is None else min(prev.step * 2, COOLDOWN_MAX_S)
            entry = Cooldown(self._clock() + step, step)
            self._state = MappingProxyType({**self._state, bucket: entry})
        return entry.until

    def clear(self, bucket: str) -> None:
        """A success on this login: the next rate limit starts at 30 s again."""
        with self._lock:
            self._state = MappingProxyType({k: v for k, v in self._state.items() if k != bucket})

    def until(self, bucket: str) -> float | None:
        """When this login's cool-down ends, or None when it is not cooling down."""
        with self._lock:
            entry = self._state.get(bucket)
        return entry.until if entry is not None and entry.until > self._clock() else None

    def active(self) -> frozenset[str]:
        """Every login currently cooling down."""
        now = self._clock()
        with self._lock:
            return frozenset(b for b, e in self._state.items() if e.until > now)
