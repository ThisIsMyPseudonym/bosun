"""Quota-window usage report for the dashboard and API (`llm_usage`).

Builds each login's windows (share, pace, reset, source, stale), Bosun's own share per window since its reset,
top jobs by draw, estimated logins, 75%-line notices, the one-time retired-budget notice, routing settings, and
tokens/list-price cost as detail. A stale or unknown reading is always `None` (unknown), never 0% used.
"""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING, Any

from ..registry import Conflict
from . import accounts, catalog, ledger, quota, routing, windows

if TYPE_CHECKING:
    from ..store import Store

BUDGETS_RETIRED_KEY = "ai_budgets_retired"
BUDGETS_NOTICE_KEY = "ai_budgets_notice"
WINDOW_ORDER = {"five_hour": 0, "one_day": 1, "seven_day": 2}


def _finite_round(val: float | None, digits: int = 4) -> float | None:
    if val is None or not math.isfinite(val):
        return None
    return round(val, digits)


def _window_start(r: windows.Reading, resets_at: float | None) -> float | None:
    hours = windows.window_hours(r.window)
    if resets_at is None or hours is None:
        return None
    return resets_at - hours * 3600.0


def _window_entry(store: Store, provider: str, account: str, r: windows.Reading, now: float) -> dict[str, Any]:
    is_stale = quota.stale(r, now)
    resets_at = None if is_stale else windows.valid_reset(r.window, r.resets_at, r.at)
    win_share = None if is_stale else _finite_round(quota.share(r), 4)
    win_pace = None if is_stale else _finite_round(quota.pace(r, now), 2)
    start = None if is_stale else _window_start(r, resets_at)
    bosun_draw: float | None = None
    if not is_stale and start is not None:
        bosun_draw = _finite_round(store.llm_draw(since=start, provider=provider, account=account)["window_draw"], 4)
    return {
        "window": r.window,
        "label": ledger.window_label(r.window),
        "status": r.status,
        "share": win_share,
        "pace": win_pace,
        "resets_at": resets_at,
        "reset": ledger.format_reset(resets_at) if resets_at is not None else "",
        "source": r.source,
        "at": r.at,
        "stale": is_stale,
        "bosun_share": bosun_draw,
    }


def _window_sort_key(r: windows.Reading) -> tuple[int, str]:
    return (WINDOW_ORDER.get(r.window, 10), r.window)


def _override_for(bucket: str, overrides: tuple[quota.Override, ...]) -> dict[str, Any] | None:
    mine = [o for o in overrides if o.bucket == bucket]
    if not mine:
        return None
    until = max(o.until for o in mine)
    return {"until": until, "reset": ledger.format_reset(until),
            "windows": sorted({o.window for o in mine if o.window is not None})}


def _line_hit_dict(readings: tuple[windows.Reading, ...], now: float) -> dict[str, Any] | None:
    hit = quota.line_hit(readings, now)
    if hit is None:
        return None
    return {"window": hit.window, "label": ledger.window_label(hit.window),
            "used": _finite_round(hit.used, 4), "resets_at": hit.resets_at,
            "reset": ledger.format_reset(hit.resets_at) if hit.resets_at is not None else ""}


def _demo_logins(store: Store, all_r: dict[str, tuple[windows.Reading, ...]],
                 by_login: dict[str, dict[str, Any]]) -> list[accounts.Login]:
    """Build synthetic logins from demo store rows without touching real CLIs on the machine."""
    stored_map = {(e.provider, e.id): e for e in accounts.stored(store)}
    buckets = list(dict.fromkeys([*all_r.keys(), *by_login.keys()]))
    out: list[accounts.Login] = []
    for bucket in buckets:
        try:
            provider, account = ledger.split(bucket)
        except ValueError:
            continue
        entry = stored_map.get((provider, account))
        label = entry.label if entry else ("Default" if account == accounts.DEFAULT else account)
        out.append(accounts.Login(provider, account, label, entry.home if entry else None,
                                  entry.excluded if entry else False, f"demo:{provider}"))
    order = {p: i for i, p in enumerate(catalog.CATALOG)}
    return sorted(out, key=lambda lg: (order.get(lg.provider, 99), 0 if lg.id == accounts.DEFAULT else 1, lg.id))


def _collect_logins(store: Store, all_r: dict[str, tuple[windows.Reading, ...]],
                    by_login: dict[str, dict[str, Any]], *, demo: bool) -> list[accounts.Login]:
    if demo:
        return _demo_logins(store, all_r, by_login)
    installed_logins = list(accounts.logins(store))
    seen = {lg.bucket for lg in installed_logins}
    for bucket in (*all_r.keys(), *by_login.keys()):
        if bucket in seen:
            continue
        try:
            provider, account = ledger.split(bucket)
        except ValueError:
            continue
        seen.add(bucket)
        label = "Default" if account == accounts.DEFAULT else account
        installed_logins.append(accounts.Login(provider, account, label, None, False, ""))
    return installed_logins


def budgets_retired(store: Store) -> dict[str, Any] | None:
    """The one-time retired-budget notice if still pending; None once dismissed or never needed."""
    if store.setting(BUDGETS_NOTICE_KEY) != "pending":
        return None
    raw = store.setting(BUDGETS_RETIRED_KEY)
    if not raw:
        return {"retired": True}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return {"retired": True}
    return parsed if isinstance(parsed, dict) else {"retired": True}


def dismiss_notice(store: Store) -> dict[str, Any]:
    """Mark the one-time retired-budget notice as shown (owner-only)."""
    if store.read_only:
        raise Conflict("Bosun's data is open read-only here (Store is read-only)")
    store.set_setting(BUDGETS_NOTICE_KEY, "shown")
    return {"dismissed": True}


def _login_card(store: Store, lg: accounts.Login, readings: tuple[windows.Reading, ...],
                overrides: tuple[quota.Override, ...], stats: dict[str, Any], now: float) -> dict[str, Any]:
    win_rows = [_window_entry(store, lg.provider, lg.id, r, now) for r in sorted(readings, key=_window_sort_key)]
    curr = quota.current(readings, now)
    raw_pace = quota.login_pace(readings, now)
    return {
        "bucket": lg.bucket,
        "provider": lg.provider,
        "provider_label": catalog.CATALOG[lg.provider].label,
        "account": lg.id,
        "label": lg.label,
        "excluded": lg.excluded,
        "installed": bool(lg.binary),
        "estimated": quota.estimated(lg.provider),
        "pace": _finite_round(raw_pace, 2),
        "limited": any(r.status == "rejected" for r in curr),
        "line_hit": _line_hit_dict(readings, now),
        "override": _override_for(lg.bucket, overrides),
        "windows": win_rows,
        "calls": int(stats.get("calls") or 0),
        "tokens": int(stats.get("tokens") or 0),
    }


def report(store: Store, days: float = 30, now: float | None = None, *, demo: bool = False) -> dict[str, Any]:
    """Quota windows, pace, Bosun's share, top jobs, estimated logins, notices, routing and token detail."""
    when = time.time() if now is None else now
    since = when - days * 86400.0
    detail = store.llm_usage(days=days, now=when)
    draw_summary = store.llm_draw(since=since)
    by_login = {row["bucket"]: row for row in draw_summary["by_login"]}
    all_r = ledger.all_readings(store)
    active_overrides = routing.overrides(store, now=when)
    found = _collect_logins(store, all_r, by_login, demo=demo)

    logins_out: list[dict[str, Any]] = []
    bosun_share: dict[str, dict[str, float | None]] = {}
    estimated_out: list[dict[str, Any]] = []
    for lg in found:
        card = _login_card(store, lg, all_r.get(lg.bucket, ()), active_overrides, by_login.get(lg.bucket, {}), when)
        logins_out.append(card)
        win_shares = {w["window"]: w["bosun_share"] for w in card["windows"] if not w["stale"]}
        if win_shares:
            bosun_share[lg.bucket] = win_shares
        if card["estimated"]:
            estimated_out.append({
                "bucket": lg.bucket, "provider": lg.provider, "provider_label": card["provider_label"],
                "account": lg.id, "label": lg.label, "calls": card["calls"], "tokens": card["tokens"],
            })

    route_info = {**routing.overview(store, now=when), "jobs": list(catalog.JOBS)}
    return {
        "days": days,
        "since": detail["since"],
        "until": detail["until"],
        "demo": demo,
        "logins": logins_out,
        "bosun_share": bosun_share,
        "top_jobs": draw_summary["by_job"],
        "estimated": estimated_out,
        "notices": ledger.notices(store, now=when),
        "budgets_retired": budgets_retired(store),
        "routing": route_info,
        "detail": detail,
        "totals": detail["totals"],
        "by_purpose": detail["by_purpose"],
        "cache": detail["cache"],
    }
