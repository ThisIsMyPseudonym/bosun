"""A plan's usage windows, as each CLI reports them (a port of Coho's `cliWindows.ts`).

A `Reading` is one window at one moment: its name (`five_hour`, `seven_day`, `quota:<model>`, ...), whether the
provider still allows calls, the share used where the CLI said, and when it resets where it said. Every parser
takes raw output and returns a tuple of readings; it never raises on bad input, and anything it cannot read is
dropped rather than guessed, so a malformed reply can never make a login look like it has room.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

Status = Literal["allowed", "allowed_warning", "rejected"]
Source = Literal["output", "probe", "coho", "limit"]

STATUSES: tuple[Status, ...] = ("allowed", "allowed_warning", "rejected")
SOURCES: tuple[Source, ...] = ("output", "probe", "coho", "limit")
NAME = re.compile(r"[a-z0-9_]{1,40}")
MS_EPOCH = 1e12  # an epoch at or past this is in milliseconds
MAX_JSON_CHARS = 1 << 17  # cap line/document length before json.loads so hostile input stays cheap
RESET_SLACK_S = 3600.0  # clock skew / top-of-hour rounding beyond a window's nominal length
MAX_RESET_S = 31 * 86400.0  # upper bound on resets for windows whose length is not in their name


@dataclass(frozen=True)
class Reading:
    window: str
    status: Status
    used: float | None  # share used, 0 to 1; None when the CLI gave no figure
    resets_at: float | None  # epoch seconds; None when the CLI did not say
    at: float  # when it was read
    source: Source = "output"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def fraction(value: object) -> float | None:
    """A fraction that runs past 1 when usage goes over a cap; capped at 1."""
    f = _number(value)
    return None if f is None or f < 0 else min(1.0, f)


def epoch(value: object) -> float | None:
    """An epoch (seconds or milliseconds) or an ISO time, as epoch seconds."""
    f = _number(value)
    if f is not None:
        return None if f <= 0 else (f / 1000 if f >= MS_EPOCH else f)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo else None  # a time with no zone is ambiguous


def status_of(value: object) -> Status | None:
    return cast(Status, value) if isinstance(value, str) and value in STATUSES else None


def name_of(value: object, fallback: str = "window") -> str:
    return value if isinstance(value, str) and NAME.fullmatch(value) else fallback


_RANK = {s: i for i, s in enumerate(STATUSES)}


def worse(a: Status, b: Status) -> Status:
    return a if _RANK[a] >= _RANK[b] else b


def json_object(line: str) -> dict[str, Any] | None:
    """A line that is a JSON object, else None."""
    if len(line) > MAX_JSON_CHARS:
        return None
    try:
        value = json.loads(line.strip())
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def json_lines(raw: str, marker: re.Pattern[str]) -> tuple[dict[str, Any], ...]:
    """Each line matching `marker` that is a JSON object."""
    found = (json_object(line) for line in raw.splitlines() if len(line) <= MAX_JSON_CHARS and marker.search(line))
    return tuple(o for o in found if o is not None)


def _higher(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    return a if b is None else max(a, b)


def _merge(a: Reading, b: Reading) -> Reading:
    """Combine two readings of the same window safely: worse status, higher used, later reset."""
    return Reading(b.window, worse(a.status, b.status), _higher(a.used, b.used),
                   _higher(a.resets_at, b.resets_at), max(a.at, b.at), b.source)


def latest_per_window(readings: list[Reading]) -> tuple[Reading, ...]:
    """One reading per window in first-seen order; collisions keep the worse status and higher used."""
    seen: dict[str, Reading] = {}
    for r in readings:
        prev = seen.get(r.window)
        seen[r.window] = _merge(prev, r) if prev is not None else r
    return tuple(seen.values())


# --- Claude ------------------------------------------------------------------------------------------------------


def _unified(name: str, w: dict[str, Any], at: float) -> Reading | None:
    used, resets = fraction(w.get("utilization")), valid_reset(name, epoch(w.get("resetsAt")), at)
    if used is None and resets is None:
        return None
    over = _number(w.get("surpassedThreshold"))
    status = status_of(w.get("status")) or ("rejected" if over is not None and over >= 1 else "allowed")
    return Reading(name, status, used, resets, at)


def _claude_event(info: dict[str, Any], at: float) -> list[Reading]:
    status = status_of(info.get("status"))
    if status is None:
        return []
    win = name_of(info.get("rateLimitType"))
    top = Reading(win, status, fraction(info.get("utilization")), valid_reset(win, epoch(info.get("resetsAt")), at), at)
    out = {top.window: top}
    unified = info.get("unifiedWindows")
    for name, w in (unified.items() if isinstance(unified, dict) else ()):
        if not isinstance(w, dict) or not NAME.fullmatch(str(name)):
            continue
        r = _unified(str(name), w, at)
        if r is None:
            continue
        # The per-window entry is the fuller one: it replaces the top-level one, keeping the worse status.
        same = out.get(r.window)
        out[r.window] = Reading(r.window, worse(same.status, r.status), r.used, r.resets_at, at) if same else r
    return list(out.values())


_CLAUDE_MARK = re.compile("rate_limit_event")


def parse_claude(raw: str, at: float) -> tuple[Reading, ...]:
    """Every `rate_limit_event` in Claude Code's stream-json (and the Agent SDK's), latest per window.

    The top-level fields name the window limiting the account now; `unifiedWindows` (Claude Code 2.1.3xx) holds
    one entry per window with its own utilization, reset and status.
    """
    readings: list[Reading] = []
    for ev in json_lines(raw, _CLAUDE_MARK):
        info = ev.get("rate_limit_info")
        if ev.get("type") == "rate_limit_event" and isinstance(info, dict):
            readings.extend(_claude_event(info, at))
    return latest_per_window(readings)


_MIN_NAME = re.compile(r"(\d+)_min")


def window_hours(window: str) -> float | None:
    """A window's length in hours, where its name says (Coho's `windowHoursOf`); None for `quota:<model>` and the like."""
    if window == "five_hour":
        return 5.0
    if window.startswith("seven_day"):
        return 168.0
    if window == "one_day":
        return 24.0
    m = _MIN_NAME.fullmatch(window)
    return int(m[1]) / 60 if m and int(m[1]) > 0 else None


def reset_horizon(window: str) -> float:
    """Maximum plausible seconds between a reading and its window's reset."""
    hours = window_hours(window)
    return hours * 3600.0 + RESET_SLACK_S if hours is not None else MAX_RESET_S


def valid_reset(window: str, resets_at: float | None, at: float) -> float | None:
    """Keep `resets_at` only when it lies within the window's horizon from `at`; else drop it."""
    if resets_at is None:
        return None
    return resets_at if resets_at <= at + reset_horizon(window) else None


# --- Codex -------------------------------------------------------------------------------------------------------

CODEX_WARNING = 0.9  # Codex sends no status: at or past 90% is a warning, at 100% a refusal
_MINUTES = {300: "five_hour", 10_080: "seven_day", 1_440: "one_day"}


def window_for_minutes(minutes: object, fallback: str = "window") -> str:
    """A window's name from its length: Codex says minutes, not a type."""
    m = _number(minutes)
    if m is None or m <= 0:
        return fallback
    return _MINUTES.get(round(m), f"{round(m)}_min")


def _pick(o: dict[str, Any], *keys: str) -> object:
    for k in keys:
        if o.get(k) is not None:
            return o[k]
    return None


def _codex_slot(w: dict[str, Any], at: float, fallback: str = "window") -> Reading | None:
    percent = _number(_pick(w, "usedPercent", "used_percent"))
    used = min(1.0, percent / 100) if percent is not None and percent >= 0 else None
    in_seconds = _number(_pick(w, "resets_in_seconds", "resetsInSeconds"))
    minutes = _pick(w, "windowDurationMins", "window_duration_mins", "window_minutes", "windowMinutes")
    win = window_for_minutes(minutes, fallback)
    resets = epoch(_pick(w, "resetsAt", "resets_at"))
    if resets is None and in_seconds is not None and in_seconds >= 0:
        resets = at + in_seconds
    resets = valid_reset(win, resets, at)
    if used is None and resets is None:
        return None
    status: Status = ("rejected" if used is not None and used >= 1
                      else "allowed_warning" if used is not None and used >= CODEX_WARNING else "allowed")
    return Reading(win, status, used, resets, at)


def _codex_snapshot(value: object) -> dict[str, Any] | None:
    """The object holding `primary`/`secondary`, from a token_count event or an app-server message."""
    if not isinstance(value, dict):
        return None
    inner = _pick(value, "rateLimits", "rate_limits", "result")
    inner = inner if isinstance(inner, dict) else value
    nested = _pick(inner, "rateLimits", "rate_limits")
    snapshot = nested if nested is not None else inner
    return snapshot if isinstance(snapshot, dict) else None


def parse_codex_snapshot(value: object, at: float) -> tuple[Reading, ...]:
    """Codex's rate-limit snapshot: `primary` is the short window (5 hours on ChatGPT plans), `secondary` the week."""
    snapshot = _codex_snapshot(value)
    if snapshot is None:
        return ()
    found = (_codex_slot(w, at, key) for key in ("primary", "secondary") if isinstance(w := snapshot.get(key), dict))
    return tuple(r for r in found if r is not None)


_CODEX_MARK = re.compile(r"rate_?[lL]imits")


def parse_codex(raw: str, at: float) -> tuple[Reading, ...]:
    """Every rate-limit snapshot in Codex's JSON lines (`exec --json` token_count events, app-server messages)."""
    readings: list[Reading] = []
    for ev in json_lines(raw, _CODEX_MARK):
        payload = _pick(ev, "payload", "msg", "params", "result")
        readings.extend(parse_codex_snapshot(payload if payload is not None else ev, at))
    return latest_per_window(readings)


# --- Antigravity -------------------------------------------------------------------------------------------------

_SLUG = re.compile(r"[^a-z0-9.-]+")
_PERCENT_CELL = re.compile(r"\d+(?:\.\d+)?%|0?\.\d+|1(?:\.0+)?")
_DATE_CELL = re.compile(r"\d{4}-\d{2}-\d{2}")


def quota_window(name: object) -> str:
    """`quota:<model>` from a model's display name, or plain `quota` when it has none."""
    slug = _SLUG.sub("-", name.strip().lower())[:40].strip("-") if isinstance(name, str) else ""
    return f"quota:{slug}" if slug else "quota"


def _remaining(value: object) -> float | None:
    """A remaining share, 0-1, from a fraction, a percentage or a string such as "10%"."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = float(text[:-1]) / 100 if text.endswith("%") else float(text)
        except ValueError:
            return None
    f = _number(value)
    if f is None or f < 0:
        return None
    return min(1.0, f / 100 if f > 1 else f)


def _quota(name: object, remaining: object, reset: object, at: float) -> Reading | None:
    left = _remaining(remaining)
    if left is None:
        return None
    used = 1.0 - left
    win = quota_window(name)
    status: Status = "rejected" if used >= 1 else "allowed_warning" if used >= CODEX_WARNING else "allowed"
    return Reading(win, status, used, valid_reset(win, epoch(reset), at), at, "probe")


def _agy_entries(parsed: object) -> list[object]:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return next((v for v in parsed.values() if isinstance(v, list)), [parsed])
    return []


def _agy_json(parsed: object, at: float) -> list[Reading]:
    out: list[Reading] = []
    for item in _agy_entries(parsed):
        if not isinstance(item, dict):
            continue
        q = _pick(item, "quotaInfo", "quota_info")
        q = q if isinstance(q, dict) else item
        remaining = _pick(q, "remainingFraction", "remaining_fraction")
        if remaining is None:
            percent = _number(_pick(q, "remainingPercentage", "remaining_percentage"))
            remaining = percent / 100 if percent is not None else None
        r = _quota(_pick(item, "model", "label", "name", "displayName"), remaining,
                   _pick(q, "resetTime", "reset_time", "resetsAt"), at)
        if r is not None:
            out.append(r)
    return out


def _agy_lines(text: str, at: float) -> list[Reading]:
    out: list[Reading] = []
    for line in text.splitlines():
        if len(line) > MAX_JSON_CHARS:
            continue
        cells = [c.strip() for c in line.split("\t")]
        share_cell = next((c for c in cells[1:] if _PERCENT_CELL.fullmatch(c)), None)
        if len(cells) < 2 or share_cell is None:
            continue
        reset = next((c for c in cells if _DATE_CELL.search(c) and epoch(c) is not None), None)
        r = _quota(cells[0], share_cell, reset, at)
        if r is not None:
            out.append(r)
    return out


def parse_antigravity(raw: str, at: float) -> tuple[Reading, ...]:
    """Antigravity's `/usage` report: JSON quota entries (a remaining fraction and a reset time), or lines of
    `name<TAB>remaining<TAB>reset`. Built from its changelog's description; no recorded reply exists yet."""
    text = raw.strip()
    if text.startswith(("{", "[")) and len(text) <= MAX_JSON_CHARS:
        try:
            return latest_per_window(_agy_json(json.loads(text), at))
        except (ValueError, RecursionError):
            pass  # not JSON after all: read it as lines
    return latest_per_window(_agy_lines(text, at))
