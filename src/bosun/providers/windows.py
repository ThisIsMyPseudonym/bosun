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
NAME = re.compile(r"^[a-z0-9_]{1,40}$")
MS_EPOCH = 1e12  # an epoch at or past this is in milliseconds


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


def share(value: object) -> float | None:
    """A share used as 0-1, from a fraction or a percentage (above 1 means percent), capped at 1."""
    f = _number(value)
    if f is None or f < 0:
        return None
    return min(1.0, f / 100 if f > 1 else f)


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
    return value if isinstance(value, str) and NAME.match(value) else fallback


_RANK = {s: i for i, s in enumerate(STATUSES)}


def worse(a: Status, b: Status) -> Status:
    return a if _RANK[a] >= _RANK[b] else b


def json_object(line: str) -> dict[str, Any] | None:
    """A line that is a JSON object, else None."""
    try:
        value = json.loads(line.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def json_lines(raw: str, marker: re.Pattern[str]) -> tuple[dict[str, Any], ...]:
    """Each line matching `marker` that is a JSON object."""
    found = (json_object(line) for line in raw.splitlines() if marker.search(line))
    return tuple(o for o in found if o is not None)


def latest_per_window(readings: list[Reading]) -> tuple[Reading, ...]:
    """The last reading of each window, in the order the windows first appeared."""
    seen: dict[str, Reading] = {}
    for r in readings:
        seen[r.window] = r
    return tuple(seen.values())


# --- Claude ------------------------------------------------------------------------------------------------------


def _unified(name: str, w: dict[str, Any], at: float) -> Reading | None:
    used, resets = fraction(w.get("utilization")), epoch(w.get("resetsAt"))
    if used is None and resets is None:
        return None
    over = _number(w.get("surpassedThreshold"))
    status = status_of(w.get("status")) or ("rejected" if over is not None and over >= 1 else "allowed")
    return Reading(name, status, used, resets, at)


def _claude_event(info: dict[str, Any], at: float) -> list[Reading]:
    status = status_of(info.get("status"))
    if status is None:
        return []
    top = Reading(name_of(info.get("rateLimitType")), status, share(info.get("utilization")),
                  epoch(info.get("resetsAt")), at)
    out = {top.window: top}
    unified = info.get("unifiedWindows")
    for name, w in (unified.items() if isinstance(unified, dict) else ()):
        if not isinstance(w, dict) or not NAME.match(str(name)):
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


# --- Codex -------------------------------------------------------------------------------------------------------

CODEX_WARNING = 0.9  # Codex sends no status: at or past 90% is a warning, at 100% a refusal
_MINUTES = {300: "five_hour", 10_080: "seven_day", 1_440: "one_day"}


def window_for_minutes(minutes: object) -> str:
    """A window's name from its length: Codex says minutes, not a type."""
    m = _number(minutes)
    if m is None or m <= 0:
        return "window"
    return _MINUTES.get(round(m), f"{round(m)}_min")


def _pick(o: dict[str, Any], *keys: str) -> object:
    for k in keys:
        if o.get(k) is not None:
            return o[k]
    return None


def _codex_slot(w: dict[str, Any], at: float) -> Reading | None:
    percent = _number(_pick(w, "usedPercent", "used_percent"))
    used = min(1.0, percent / 100) if percent is not None and percent >= 0 else None
    in_seconds = _number(_pick(w, "resets_in_seconds", "resetsInSeconds"))
    resets = epoch(_pick(w, "resetsAt", "resets_at"))
    if resets is None and in_seconds is not None and in_seconds >= 0:
        resets = at + in_seconds
    if used is None and resets is None:
        return None
    status: Status = ("rejected" if used is not None and used >= 1
                      else "allowed_warning" if used is not None and used >= CODEX_WARNING else "allowed")
    minutes = _pick(w, "windowDurationMins", "window_duration_mins", "window_minutes", "windowMinutes")
    return Reading(window_for_minutes(minutes), status, used, resets, at)


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
    slots = (snapshot.get(k) for k in ("primary", "secondary"))
    found = (_codex_slot(w, at) for w in slots if isinstance(w, dict))
    return tuple(r for r in found if r is not None)


_CODEX_MARK = re.compile(r"rate_?[lL]imits")


def parse_codex(raw: str, at: float) -> tuple[Reading, ...]:
    """Every rate-limit snapshot in Codex's JSON lines (`exec --json` token_count events, app-server messages)."""
    readings: list[Reading] = []
    for ev in json_lines(raw, _CODEX_MARK):
        payload = _pick(ev, "payload", "msg", "params", "result")
        readings.extend(parse_codex_snapshot(payload if payload is not None else ev, at))
    return latest_per_window(readings)
