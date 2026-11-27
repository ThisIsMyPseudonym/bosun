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

from ..registry import Invalid
from . import accounts, catalog, ledger, quota, router, windows

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


def overrides(store: Store, now: float | None = None) -> tuple[quota.Override, ...]:
    """Active 75%-line overrides from the store; malformed, expired or far-future entries are dropped."""
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
    for bucket, raw_until in parsed.items():
        if not isinstance(bucket, str):
            continue
        try:
            ledger.split(bucket)
        except ValueError:
            continue
        until = _finite(raw_until)
        if until is not None and when < until <= when + windows.MAX_RESET_S:
            kept.append(quota.Override(bucket, until))
    return tuple(kept)


def _override_expiry(store: Store, bucket: str, when: float) -> float:
    """When an override on `bucket` ends: the latest hit window's reset, or 24 h when undated or no hit exists."""
    hits: list[quota.Hit] = []
    for r in quota.current(ledger.readings(store, bucket), when):
        used = quota.share(r)
        if used is not None and (used >= quota.LINE or r.status == "rejected"):
            hits.append(quota.Hit(r.window, used, windows.valid_reset(r.window, r.resets_at, r.at)))
    if not hits:
        return when + quota.OVERRIDE_UNDATED_S
    return max(quota.override_until(h, when) for h in hits)


def _require_bucket(bucket: str, known: set[str]) -> str:
    if bucket not in known:
        raise Invalid(f"Unknown AI login: {bucket}")
    return bucket


def set_override(store: Store, bucket: str, on: bool, now: float | None = None) -> dict[str, Any]:
    """Turn the 75%-line override on (until the hit window's reset, or 24 h) or off for one login."""
    if store.read_only:
        raise RuntimeError("Store is read-only")
    known = {lg.bucket for lg in accounts.logins(store)}
    _require_bucket(bucket, known)
    when = time.time() if now is None else now
    with _writing:
        active = {o.bucket: o.until for o in overrides(store, now=when)}
        until: float | None = None
        if on:
            expiry = _override_expiry(store, bucket, when)
            active[bucket] = expiry
            until = expiry
        else:
            active.pop(bucket, None)
        store.set_setting(OVERRIDES_SETTING, json.dumps(active))
    return {"bucket": bucket, "on": on, "until": until}


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
    if store.read_only:
        raise RuntimeError("Store is read-only")
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
        for job, target in (pins or {}).items():
            if target is None or target == "":
                new_pins.pop(job, None)
            else:
                new_pins[job] = target
        for default_job in router.DEFAULT_PINS:
            if default_job not in new_pins:
                new_pins[default_job] = None  # preserve explicit unpin against DEFAULT_PINS
        new_job_modes = dict(rs.job_modes)
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
        "overrides": [{"bucket": o.bucket, "until": o.until} for o in active],
    }
