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


def json_lines(raw: str, marker: str) -> tuple[dict[str, Any], ...]:
    """Each line holding `marker` that is a JSON object."""
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if marker not in line:
            continue
        try:
            value = json.loads(line.strip())
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return tuple(out)


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


def parse_claude(raw: str, at: float) -> tuple[Reading, ...]:
    """Every `rate_limit_event` in Claude Code's stream-json (and the Agent SDK's), latest per window.

    The top-level fields name the window limiting the account now; `unifiedWindows` (Claude Code 2.1.3xx) holds
    one entry per window with its own utilization, reset and status.
    """
    readings: list[Reading] = []
    for ev in json_lines(raw, "rate_limit_event"):
        info = ev.get("rate_limit_info")
        if ev.get("type") == "rate_limit_event" and isinstance(info, dict):
            readings.extend(_claude_event(info, at))
    return latest_per_window(readings)
