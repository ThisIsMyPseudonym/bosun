"""When a provider refuses a login: usage-limit replies (a port of Coho's `llmProviders.ts`).

A usage-limit reply is not an answer, even with exit code 0 (Claude Code prints "API Error: Rate limit reached" and
exits 0). It becomes a `limit` reading, `rejected`, that resets when the reply says, or in an hour when it names no
time Bosun can read. Only call `limit_reading` on a reply that did not parse as the answer the job expected: the
words it looks for can appear in an answer about rate limits.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bosun.providers.windows import Reading

NO_RESET_S = 3600  # a limit that names no reset keeps the login out for an hour

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
    r"(?:\s*\(?\s*(?P<zone>UTC|GMT|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\b\s*\)?)?",
    re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), start=1)}
_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _iso(text: str, now: float) -> float | None:
    for m in _ISO.finditer(text):
        try:
            parsed = datetime.fromisoformat(m[1].replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo and parsed.timestamp() > now:
            return parsed.timestamp()
    return None


def _epoch(text: str, now: float) -> float | None:
    for m in _EPOCH.finditer(text):
        t = float(m[1])
        if t > now:
            return t
    return None


def _relative(text: str, now: float) -> float | None:
    for m in _RELATIVE.finditer(text):
        seconds = sum(float(n) * _SECONDS[unit[0].lower()] for n, unit in _PART.findall(m[1]))
        if seconds > 0:
            return now + seconds
    return None


def _zone(name: str | None, fallback: tzinfo | None) -> tzinfo | None:
    """The zone a reply names ("(America/Chicago)", "UTC"), else the fallback (None: the machine's own)."""
    if name is None:
        return fallback
    if name.upper() in ("UTC", "GMT"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return fallback


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
    hm, zone = _hour(m), _zone(m["zone"], tz)
    if hm is None:
        return None
    today = datetime.fromtimestamp(now, zone)
    try:
        at = today.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if m["mon"]:
            at = at.replace(year=int(m["year"] or at.year), month=_MONTHS[m["mon"].lower()[:3]], day=int(m["day"]))
    except ValueError:  # Feb 30
        return None
    if at.timestamp() > now:
        return at.timestamp()
    if m["year"]:
        return None  # a named date already past says nothing about when
    wall = at.replace(tzinfo=None)  # step the wall clock, then read it in the zone again (DST moves the offset)
    try:
        wall = wall.replace(year=wall.year + 1) if m["mon"] else wall + timedelta(days=1)
    except ValueError:  # Feb 29 next year
        return None
    return wall.replace(tzinfo=zone).timestamp()


def _clock(text: str, now: float, tz: tzinfo | None) -> float | None:
    """A clock time ("resets 3:50pm", "try again at Oct 24th, 2026 3:05 PM"), in the zone it names or `tz`.
    A time with no date already past means tomorrow; a date with no year already past means next year."""
    for m in _CLOCK.finditer(text):
        found = _clock_match(m, now, tz)
        if found is not None:
            return found
    return None


def reset_time(text: str, now: float, tz: tzinfo | None = None) -> float | None:
    """When a limit message says the limit resets (epoch seconds), or None when it names no time Bosun can read."""
    flat = " ".join(text.split())
    for found in (_iso(flat, now), _epoch(flat, now), _relative(flat, now), _clock(flat, now, tz)):
        if found is not None:
            return found
    return None


def limit_reading(text: str, now: float, tz: tzinfo | None = None) -> Reading | None:
    """A usage-limit reply as a `rejected` reading of the `limit` window, or None when the text is not one."""
    if not text or not LIMIT_WORDS.search(text):
        return None
    resets = reset_time(text, now, tz)
    return Reading("limit", "rejected", None, resets if resets is not None else now + NO_RESET_S, now, "limit")
