"""Owner-only persistence for AI routing settings and 75%-line overrides.

Keeps `router.py` pure (apart from reading `settings(store)`) while giving the dashboard actions one validated,
serialized read-modify-write path that respects `store.read_only` and drops malformed or expired overrides.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .. import trace
from ..registry import Conflict, Invalid
from . import accounts, catalog, contract, ledger, limits, quota, router, windows

if TYPE_CHECKING:
    from ..store import Store

OVERRIDES_SETTING = "ai_quota_overrides"
_writing = threading.Lock()


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _override_entries(bucket: str, raw: object, when: float) -> list[quota.Override]:
    """One login's stored `{window: until}`; an old bare `until` (no window), a bad window name or an `until`
    past what that window could last is dropped, so the line comes back rather than staying lifted."""
    if not isinstance(raw, dict):
        return []
    kept: list[quota.Override] = []
    for window, raw_until in raw.items():
        if not isinstance(window, str) or not ledger.valid_window(window):
            continue
        until = _finite(raw_until)
        o = quota.Override(bucket, until, window) if until is not None else None
        if o is not None and quota.lifted(o, bucket, when):
            kept.append(o)
    return kept


def overrides(store: Store, now: float | None = None) -> tuple[quota.Override, ...]:
    """Active 75%-line overrides from the store, one per login and window; malformed, expired or far-future
    entries are dropped."""
    raw = store.setting(OVERRIDES_SETTING)
    if not raw or len(raw) > router.MAX_SETTING_CHARS:
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    when = time.time() if now is None else now
    kept: list[quota.Override] = []
    for bucket, entry in parsed.items():
        if isinstance(bucket, str) and router._valid_bucket(bucket) is not None:
            kept.extend(_override_entries(bucket, entry, when))
    return tuple(kept)


def _override_windows(store: Store, bucket: str, when: float) -> dict[str, float]:
    """Each window now at or past the line, with when an override on it ends: that window's reset, or a day when
    undated. Empty when nothing is held back."""
    hits = (quota.Hit(r.window, used, windows.valid_reset(r.window, r.resets_at, r.at))
            for r in quota.current(ledger.readings(store, bucket), when)
            if (used := quota.share(r)) is not None and (used >= quota.LINE or r.status == "rejected"))
    return {h.window: quota.override_until(h, when) for h in hits}


def _require_bucket(bucket: str, known: set[str]) -> str:
    if bucket not in known:
        raise Invalid(f"Unknown AI login: {bucket}")
    return bucket


def _require_writable(store: Store) -> None:
    if store.read_only:
        raise Conflict("Bosun's data is open read-only here (Store is read-only); change this from the dashboard")


def _saved_overrides(store: Store, when: float) -> dict[str, dict[str, float]]:
    saved: dict[str, dict[str, float]] = {}
    for o in overrides(store, now=when):
        if o.window is not None:
            saved.setdefault(o.bucket, {})[o.window] = o.until
    return saved


def set_override(store: Store, bucket: str, on: bool, now: float | None = None) -> dict[str, Any]:
    """Turn the 75%-line override on or off for one login. On lifts the line only for the windows at or past it
    now, each until its own reset (a day when undated); with no window at the line it is refused."""
    _require_writable(store)
    _require_bucket(bucket, {lg.bucket for lg in accounts.logins(store)})
    when = time.time() if now is None else now
    with _writing:
        active = _saved_overrides(store, when)
        lifted: dict[str, float] = {}
        if on:
            lifted = _override_windows(store, bucket, when)
            if not lifted:
                raise Invalid(f"Nothing is held back on {bucket}: no usage window is at 75%")
            active[bucket] = lifted
        else:
            active.pop(bucket, None)
        store.set_setting(OVERRIDES_SETTING, json.dumps(active))
    return {"bucket": bucket, "on": on, "until": max(lifted.values()) if lifted else None, "windows": lifted}


def _validate_pins(pins: Mapping[str, str | None], known: set[str]) -> None:
    for job, bucket in pins.items():
        if job not in catalog.JOBS:
            raise Invalid(f"Unknown AI job: {job}")
        if bucket is not None and bucket != "":
            _require_bucket(bucket, known)


def _validate_job_modes(job_modes: Mapping[str, str | None], known: set[str]) -> None:
    for job, val in job_modes.items():
        if job not in catalog.JOBS:
            raise Invalid(f"Unknown AI job: {job}")
        if val is None or val == "" or val in router.MODES:
            continue
        if val.startswith("pinned:"):
            _require_bucket(val.removeprefix("pinned:"), known)
            continue
        raise Invalid(f"Unknown routing mode: {val}")


def update(store: Store, *, mode: str | None = None, order: list[str] | None = None,
           pins: Mapping[str, str | None] | None = None,
           job_modes: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    """Validate and save routing changes; omitted fields keep their stored values."""
    _require_writable(store)
    known = {lg.bucket for lg in accounts.logins(store)}
    if mode is not None and mode not in router.MODES:
        raise Invalid(f"Unknown routing mode: {mode}")
    if order is not None:
        for bucket in order:
            _require_bucket(bucket, known)
    if pins is not None:
        _validate_pins(pins, known)
    if job_modes is not None:
        _validate_job_modes(job_modes, known)
    with _writing:
        rs = router.settings(store)
        new_mode: str = rs.mode if mode is None else mode
        new_order = list(rs.order) if order is None else list(dict.fromkeys(order))
        new_pins: dict[str, str | None] = dict(rs.pins)
        new_job_modes = dict(rs.job_modes)
        for job, target in (pins or {}).items():
            if target is None or target == "":
                new_pins.pop(job, None)
            else:
                new_pins[job] = target
                new_job_modes.pop(job, None)  # a new pin is the latest choice; a per-job mode would outrank it
        for default_job in router.DEFAULT_PINS:
            if default_job not in new_pins:
                new_pins[default_job] = None  # preserve explicit unpin against DEFAULT_PINS
        for job, val in (job_modes or {}).items():
            if val is None or val == "":
                new_job_modes.pop(job, None)
            else:
                new_job_modes[job] = val
        payload = {"mode": new_mode, "order": new_order, "pins": new_pins, "job_modes": new_job_modes}
        store.set_setting(router.SETTING, json.dumps(payload))
    return overview(store)


def overview(store: Store, now: float | None = None) -> dict[str, Any]:
    """Current routing settings and active 75%-line overrides for the dashboard."""
    rs = router.settings(store)
    active = overrides(store, now=now)
    return {
        "mode": rs.mode,
        "order": list(rs.order),
        "pins": dict(rs.pins),
        "job_modes": dict(rs.job_modes),
        "overrides": [{"bucket": o.bucket, "window": o.window, "until": o.until} for o in active],
    }


def context(store: Store, *, cooldowns: limits.Cooldowns | None = None, now: float | None = None,
            logins: tuple[accounts.Login, ...] | None = None) -> router.Context:
    """Build a pure `router.Context` from the store, installed CLIs, contract verdicts, ledger and cool-downs."""
    when = time.time() if now is None else now
    found = accounts.logins(store) if logins is None else logins
    contracts: dict[str, contract.ContractStatus] = {}
    for lg in found:
        if lg.binary and lg.provider not in contracts:
            contracts[lg.provider] = contract.check(store, lg.provider, lg.binary, now=lambda: when)
    all_r = ledger.all_readings(store)
    if not store.read_only:
        for lg in found:
            if lg.bucket not in all_r and ledger.seed_from_coho(store, lg.bucket, now=when):
                all_r[lg.bucket] = ledger.readings(store, lg.bucket)
    cool_map: dict[str, float] = {}
    if cooldowns is not None:
        for lg in found:
            if (until := cooldowns.until(lg.bucket)) is not None:
                cool_map[lg.bucket] = until
    return router.Context(found, contracts, all_r, cool_map, overrides(store, now=when), when, router.settings(store),
                          background_calls(store, when))


def background_calls(store: Store, now: float) -> dict[str, tuple[int, int]]:
    """Background AI calls actually made per login: (in the current refresh's trace, in the last 24 hours)."""
    jobs = tuple(name for name, j in catalog.JOBS.items() if j.background)
    run_id = trace.current_trace_id()
    with store._tx() as db:
        rows = db.execute(
            "SELECT COALESCE(NULLIF(provider, ''), 'claude') AS p, COALESCE(NULLIF(account, ''), 'default') AS a, "
            "SUM(CASE WHEN ? != '' AND trace_id = ? THEN 1 ELSE 0 END) AS refresh, COUNT(*) AS day "
            f"FROM llm_calls WHERE ts >= ? AND COALESCE(NULLIF(job, ''), purpose) IN ({','.join('?' * len(jobs))}) "
            "AND outcome NOT IN ('cached', 'skipped') GROUP BY p, a",
            (run_id, run_id, now - 86400, *jobs)).fetchall()
    return {(r["p"] if r["a"] == accounts.DEFAULT else f"{r['p']}@{r['a']}"): (int(r["refresh"]), int(r["day"]))
            for r in rows}
