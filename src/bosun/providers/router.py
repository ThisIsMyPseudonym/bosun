"""Pure AI provider routing: settings, candidate filtering, preference/pace ordering, and failover.

Everything here is pure apart from reading (and saving) the routing setting from the `Store`: tests build a
`Context` by hand with no store and no clock. Invalid buckets, modes and pins in stored settings are dropped,
never raised, and nothing here may ever make a login look like it has more room or bring it back sooner.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Mapping, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

from . import accounts, catalog, contract, ledger, quota, windows

if TYPE_CHECKING:
    from ..store import Store

SETTING = "ai_routing"
Mode = Literal["default", "balance"]
MODES: tuple[Mode, ...] = ("default", "balance")
SkipReason = Literal["capability", "not_installed", "excluded", "contract", "limit", "cooldown", "line"]
DEFAULT_PINS: Mapping[str, str] = MappingProxyType({"fix": "claude"})
MAX_SETTING_CHARS = 1 << 16  # cap JSON size before parsing


@dataclass(frozen=True)
class RoutingSettings:
    mode: Mode
    order: tuple[str, ...]
    pins: Mapping[str, str]
    job_modes: Mapping[str, str]

    @classmethod
    def default(cls) -> RoutingSettings:
        return cls("default", (), DEFAULT_PINS, MappingProxyType({}))


def _valid_bucket(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        ledger.split(value)
    except ValueError:
        return None
    return value


def _clean_order(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(dict.fromkeys(b for item in raw if (b := _valid_bucket(item)) is not None))


def _clean_pins(raw: object) -> Mapping[str, str]:
    pins: dict[str, str] = dict(DEFAULT_PINS)
    if not isinstance(raw, Mapping):
        return MappingProxyType(pins)
    for job, bucket in raw.items():
        if not isinstance(job, str) or job not in catalog.JOBS:
            continue
        if bucket is None or bucket == "":
            pins.pop(job, None)
        elif (clean := _valid_bucket(bucket)) is not None:
            pins[job] = clean
    return MappingProxyType(pins)


def _clean_job_mode(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if value in MODES:
        return value
    if value.startswith("pinned:") and _valid_bucket(value.removeprefix("pinned:")) is not None:
        return value
    return None


def _clean_job_modes(raw: object) -> Mapping[str, str]:
    if not isinstance(raw, Mapping):
        return MappingProxyType({})
    cleaned = {job: mode for job, val in raw.items()
               if isinstance(job, str) and job in catalog.JOBS and (mode := _clean_job_mode(val)) is not None}
    return MappingProxyType(cleaned)


def clean_settings(raw: Any) -> RoutingSettings:
    """Clean a parsed JSON routing setting: unknown modes, jobs and buckets are dropped, never raised."""
    if not isinstance(raw, Mapping):
        return RoutingSettings.default()
    raw_mode = raw.get("mode")
    mode: Mode = cast(Mode, raw_mode) if isinstance(raw_mode, str) and raw_mode in MODES else "default"
    return RoutingSettings(mode, _clean_order(raw.get("order")), _clean_pins(raw.get("pins")),
                           _clean_job_modes(raw.get("job_modes")))


def settings(store: Store) -> RoutingSettings:
    """The owner's routing settings from the store, cleaned into safe defaults."""
    raw = store.setting(SETTING)
    if not raw or len(raw) > MAX_SETTING_CHARS:
        return RoutingSettings.default()
    try:
        return clean_settings(json.loads(raw))
    except (ValueError, RecursionError):
        return RoutingSettings.default()


# --- pure routing ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    logins: tuple[accounts.Login, ...]
    contracts: Mapping[str, contract.ContractStatus]
    readings: Mapping[str, tuple[windows.Reading, ...]]
    cooldowns: AbstractSet[str] | Mapping[str, float]
    overrides: tuple[quota.Override, ...]
    now: float
    settings: RoutingSettings


@dataclass(frozen=True)
class Candidate:
    provider: str
    bucket: str
    login: accounts.Login
    pace: float | None = None


@dataclass(frozen=True)
class Skip:
    bucket: str
    reason: SkipReason
    detail: str
    hit: quota.Hit | None = None


@dataclass(frozen=True)
class Route:
    job: str
    candidates: tuple[Candidate, ...]
    skipped: tuple[Skip, ...]
    mode: str
    fell_back_from: str | None = None


def _capability_detail(job: catalog.Job, provider: str) -> str:
    spec = catalog.CATALOG[provider]
    missing = sorted(job.needs - spec.capabilities)
    if missing:
        return f"{spec.label} lacks {', '.join(missing)}"
    return f"{spec.label} is not locked down for untrusted input"


def _window_label(window: str) -> str:
    if window == "five_hour":
        return "5-hour limit"
    if window == "seven_day":
        return "weekly limit"
    if window.startswith("seven_day_"):
        return f"weekly ({window.removeprefix('seven_day_')}) limit"
    if window == "one_day":
        return "daily limit"
    if window.startswith("quota:"):
        return f"{window.removeprefix('quota:')} quota"
    return "usage limit" if window == "limit" else f"{window} limit"


def _format_reset(resets_at: float) -> str:
    return datetime.fromtimestamp(resets_at).astimezone().strftime("%a %I:%M %p").replace(" 0", " ")


def _limit_skip(bucket: str, curr: tuple[windows.Reading, ...]) -> Skip | None:
    rejected = [r for r in curr if r.status == "rejected"]
    if not rejected:
        return None
    scored = [(windows.valid_reset(r.window, r.resets_at, r.at), r) for r in rejected]
    resets, worst = max(scored, key=lambda pair: (-1.0 if pair[0] is None else pair[0], pair[1].at))
    label = _window_label(worst.window)
    detail = f"{label} reached, resets {_format_reset(resets)}" if resets is not None else f"{label} reached"
    return Skip(bucket, "limit", detail, hit=quota.Hit(worst.window, 1.0, resets))


def _cooldown_skip(bucket: str, cooldowns: AbstractSet[str] | Mapping[str, float], now: float) -> Skip | None:
    if isinstance(cooldowns, Mapping):
        until = cooldowns.get(bucket)
        if until is not None and math.isfinite(until) and until > now:
            secs = max(1, round(until - now))
            return Skip(bucket, "cooldown", f"rate limited, try again in {secs} s")
        return None
    if bucket in cooldowns:
        return Skip(bucket, "cooldown", "rate limited, cooling down")
    return None


def _line_skip(bucket: str, curr: tuple[windows.Reading, ...], ctx: Context) -> Skip | None:
    stop = quota.stop_at(bucket, ctx.overrides, ctx.now)
    hit = quota.line_hit(curr, ctx.now, stop)
    if hit is None:
        return None
    label, pct = _window_label(hit.window), f"{round(hit.used * 100)}%"
    detail = f"{label} at {pct}, resets {_format_reset(hit.resets_at)}" if hit.resets_at is not None else f"{label} at {pct}"
    return Skip(bucket, "line", detail, hit=hit)


def _check_login(job: catalog.Job, capable: frozenset[str], login: accounts.Login, ctx: Context,
                 *, clicked: bool) -> Skip | None:
    if login.provider not in capable:
        return Skip(login.bucket, "capability", _capability_detail(job, login.provider))
    spec = catalog.CATALOG[login.provider]
    if not login.binary:
        return Skip(login.bucket, "not_installed", f"{spec.label} is not installed")
    if login.excluded:
        return Skip(login.bucket, "excluded", f"{spec.label} ({login.label}) is excluded")
    status = ctx.contracts.get(login.provider)
    if status is None or not status.ok:
        detail = status.finding if status is not None and status.finding else f"{spec.label}: contract check failed"
        return Skip(login.bucket, "contract", detail)
    curr = quota.current(ctx.readings.get(login.bucket, ()), ctx.now)
    if (limit := _limit_skip(login.bucket, curr)) is not None:
        return limit
    if (cool := _cooldown_skip(login.bucket, ctx.cooldowns, ctx.now)) is not None:
        return cool
    if job.background and not clicked:
        return _line_skip(login.bucket, curr, ctx)
    return None


_CATALOG_INDEX: Mapping[str, int] = MappingProxyType({p: i for i, p in enumerate(catalog.CATALOG)})


def _check_missing(job: catalog.Job, capable: frozenset[str], bucket: str) -> Skip:
    try:
        provider, _ = ledger.split(bucket)
    except ValueError:
        return Skip(bucket, "not_installed", f"{bucket} is not a valid login")
    if provider not in capable:
        return Skip(bucket, "capability", _capability_detail(job, provider))
    return Skip(bucket, "not_installed", f"{catalog.CATALOG[provider].label} is not installed")


def _ordered_entries(ctx: Context) -> list[tuple[str, accounts.Login | None]]:
    """Preference order: owner's `order` first, then remaining logins in catalog order (default login first)."""
    valid_logins = [(i, lg) for i, lg in enumerate(ctx.logins) if lg.provider in catalog.CATALOG]
    by_bucket = {lg.bucket: lg for _, lg in valid_logins}
    entries: list[tuple[str, accounts.Login | None]] = []
    seen: set[str] = set()
    for bucket in ctx.settings.order:
        if bucket in seen or _valid_bucket(bucket) is None:
            continue
        seen.add(bucket)
        entries.append((bucket, by_bucket.get(bucket)))
    rest = sorted(valid_logins, key=lambda pair: (_CATALOG_INDEX[pair[1].provider],
                                                  0 if pair[1].id == accounts.DEFAULT else 1, pair[0]))
    for _, lg in rest:
        if lg.bucket not in seen:
            seen.add(lg.bucket)
            entries.append((lg.bucket, lg))
    return entries


def _fell_back_from(entries: list[tuple[str, accounts.Login | None]], candidates: list[Candidate],
                    skipped: list[Skip], explicit: frozenset[str]) -> str | None:
    if not candidates:
        return None
    skip_reason = {s.bucket: s.reason for s in skipped}
    for bucket, _ in entries:
        reason = skip_reason.get(bucket)
        if reason == "capability":
            continue
        if reason in ("excluded", "not_installed") and bucket not in explicit:
            continue
        return bucket if bucket != candidates[0].bucket else None
    return None


def _evaluate(job: catalog.Job, capable: frozenset[str], entries: list[tuple[str, accounts.Login | None]],
              ctx: Context, *, clicked: bool) -> tuple[list[Candidate], list[Skip]]:
    candidates: list[Candidate] = []
    skipped: list[Skip] = []
    for bucket, login in entries:
        if login is None:
            skipped.append(_check_missing(job, capable, bucket))
            continue
        skip = _check_login(job, capable, login, ctx, clicked=clicked)
        if skip is not None:
            skipped.append(skip)
        else:
            pace = quota.login_pace(ctx.readings.get(login.bucket, ()), ctx.now)
            candidates.append(Candidate(login.provider, login.bucket, login, pace))
    return candidates, skipped


UNKNOWN_PACE = 0.5


def _balance_order(candidates: list[Candidate]) -> list[Candidate]:
    """Rank candidates by lowest pace; unknown pace uses the median of known paces (0.5 if none); ties stay in
    owner's order (Python's sort is stable)."""
    known = [c.pace for c in candidates if c.pace is not None and math.isfinite(c.pace)]
    fallback = statistics.median(known) if known else UNKNOWN_PACE
    return sorted(candidates, key=lambda c: fallback if c.pace is None else c.pace)


def _job_mode(job: str, rs: RoutingSettings) -> str:
    override = rs.job_modes.get(job)
    return override if override in MODES else rs.mode


def plan(job: str, ctx: Context, *, clicked: bool = False) -> Route:
    """Filter and order candidates for `job` from pure `ctx` (no store, no clock)."""
    capable = frozenset(catalog.capable(job))
    job_spec = catalog.JOBS[job]
    entries = _ordered_entries(ctx)
    candidates, skipped = _evaluate(job_spec, capable, entries, ctx, clicked=clicked)
    mode = _job_mode(job, ctx.settings)
    if mode == "balance":
        candidates = _balance_order(candidates)
    fell_back = _fell_back_from(entries, candidates, skipped, frozenset(ctx.settings.order)) if mode == "default" else None
    return Route(job, tuple(candidates), tuple(skipped), mode, fell_back)
