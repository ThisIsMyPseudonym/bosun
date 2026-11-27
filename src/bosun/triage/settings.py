"""Owner-only settings, daily budget tracking, cursor, and quota-reset persistence for error triage."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from bosun.registry import Conflict, Invalid
from bosun.store import Store

AUTO_KEY = "triage_auto"
DAILY_LIMIT_KEY = "triage_daily_limit"
CURSOR_KEY = "triage_cursor"
QUOTA_RESETS_KEY = "triage_quota_resets"

DEFAULT_AUTO = True
DEFAULT_DAILY_LIMIT = 10
MAX_DAILY_LIMIT = 100
_DAY_S = 86400.0


@dataclass(frozen=True)
class TriageSettings:
    """Owner-only automatic error triage configuration."""

    auto: bool = DEFAULT_AUTO
    daily_limit: int = DEFAULT_DAILY_LIMIT


def _parse_limit(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_DAILY_LIMIT
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_DAILY_LIMIT
    return max(0, min(MAX_DAILY_LIMIT, val))


def read(store: Store) -> TriageSettings:
    """Read current triage settings from the store (defaults: auto=True, daily_limit=10)."""
    auto_raw = store.setting(AUTO_KEY)
    auto = DEFAULT_AUTO if auto_raw is None else (auto_raw != "0")
    return TriageSettings(auto=auto, daily_limit=_parse_limit(store.setting(DAILY_LIMIT_KEY)))


def daily_auto_used(store: Store, now: float | None = None) -> int:
    """Count background automatic model triage runs in the last 24 hours (clicks are excluded)."""
    when = time.time() if now is None else now
    cutoff = when - _DAY_S
    with store._tx() as db:
        row = db.execute(
            "SELECT COUNT(DISTINCT r.trace_id) AS n FROM runs r "
            "JOIN llm_calls l ON l.trace_id = r.trace_id "
            "WHERE r.kind = 'triage' AND r.actor = 'triage' "
            "AND l.ts >= ? AND l.outcome NOT IN ('cached', 'skipped')",
            (cutoff,),
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def remaining_budget(store: Store, now: float | None = None) -> int:
    """Remaining automatic background model triage budget for the next 24-hour window."""
    cfg = read(store)
    if not cfg.auto or cfg.daily_limit <= 0:
        return 0
    return max(0, cfg.daily_limit - daily_auto_used(store, now=now))


def overview(store: Store, now: float | None = None) -> dict[str, Any]:
    """Current triage settings and 24-hour automatic usage for the dashboard and API."""
    cfg = read(store)
    used = daily_auto_used(store, now=now)
    return {
        "auto": cfg.auto,
        "daily_limit": cfg.daily_limit,
        "daily_used": used,
        "daily_remaining": max(0, cfg.daily_limit - used),
    }


def update(
    store: Store,
    *,
    auto: bool | None = None,
    daily_limit: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate and persist owner-only triage settings; omitted fields keep their stored values."""
    if store.read_only:
        raise Conflict("Bosun's data is open read-only here (Store is read-only); change this from the dashboard")
    if daily_limit is not None and not (0 <= daily_limit <= MAX_DAILY_LIMIT):
        raise Invalid(f"daily_limit must be between 0 and {MAX_DAILY_LIMIT}")
    if auto is not None:
        store.set_setting(AUTO_KEY, "1" if auto else "0")
    if daily_limit is not None:
        store.set_setting(DAILY_LIMIT_KEY, str(int(daily_limit)))
    return overview(store, now=now)


def cursor(store: Store) -> float:
    """Last scanned failure timestamp for background triage (0.0 if never scanned)."""
    raw = store.setting(CURSOR_KEY)
    if not raw:
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        return 0.0
    return val if math.isfinite(val) and val >= 0.0 else 0.0


def set_cursor(store: Store, ts: float) -> None:
    """Persist the background triage scan cursor."""
    if not store.read_only and math.isfinite(ts) and ts >= 0.0:
        store.set_setting(CURSOR_KEY, repr(float(ts)))


def quota_resets(store: Store) -> dict[str, float]:
    """Map of failure key -> earliest Unix reset timestamp for waiting_quota rows."""
    raw = store.setting(QUOTA_RESETS_KEY)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in parsed.items():
        if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
            out[k] = float(v)
    return out


def set_quota_reset(store: Store, failure: str, reset_ts: float) -> None:
    """Record when a waiting_quota failure should be retried."""
    if store.read_only or not math.isfinite(reset_ts):
        return
    cur = quota_resets(store)
    cur[failure] = float(reset_ts)
    store.set_setting(QUOTA_RESETS_KEY, json.dumps(cur, separators=(",", ":")))


def clear_quota_reset(store: Store, failure: str) -> None:
    """Remove a failure from the waiting_quota reset map."""
    if store.read_only:
        return
    cur = quota_resets(store)
    if failure in cur:
        cur.pop(failure, None)
        store.set_setting(QUOTA_RESETS_KEY, json.dumps(cur, separators=(",", ":")))
