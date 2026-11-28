"""Same-minute coalescing of due upgrade/pin_mcp firings (up to 20 items) with overflow preservation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..registry import ActionContext, ActionError, Actions, Forbidden
from ..store import Store
from . import recheck, store as astore
from .schema import AutomationDef
from .verdict import VERDICT_DEADLINE_S

MERGE_WINDOW_S = 60.0
MERGE_CAP = 20
MERGEABLE_ACTIONS = frozenset({"upgrade", "pin_mcp"})
_SIBLING_FETCH_LIMIT = 200


@dataclass(frozen=True)
class MergeBatch:
    """One coalesced action batch plus any individually skipped or overflowed firings."""

    firing_ids: tuple[int, ...]
    keys: tuple[str, ...]
    key_by_firing: tuple[tuple[int, tuple[str, ...]], ...]
    skipped: tuple[tuple[int, str], ...]
    overflow_ids: tuple[int, ...]


def _firing_due(firing: dict[str, Any]) -> float:
    return float(firing["due"]) if firing.get("due") is not None else float(firing["created"])


def collect_due_siblings(
    store: Store, automation_id: int, lead_firing: dict[str, Any], when: float,
) -> list[dict[str, Any]]:
    """Return due firings for `automation_id` within `MERGE_WINDOW_S` of `lead_firing` (lead first)."""
    lead_id = int(lead_firing["id"])
    lead_due = _firing_due(lead_firing)
    hi = min(when, lead_due + MERGE_WINDOW_S)
    with store._tx() as db:
        rows = db.execute(
            "SELECT * FROM automation_firings "
            "WHERE automation_id = ? AND status IN ('scheduled', 'waiting_quota') "
            "AND (due IS NULL OR due <= ?) "
            "AND COALESCE(due, created) >= ? AND COALESCE(due, created) <= ? "
            "ORDER BY COALESCE(due, created) ASC, id ASC LIMIT ?",
            (automation_id, when, lead_due - 0.001, hi + 0.001, _SIBLING_FETCH_LIMIT),
        ).fetchall()
    siblings = [dict(r) for r in rows if int(r["id"]) != lead_id]
    return [lead_firing, *siblings]


def build_batch(
    store: Store, defn: AutomationDef, aid: int, candidates: list[dict[str, Any]], when: float,
) -> MergeBatch:
    """Validate each candidate independently and pack up to `MERGE_CAP` distinct keys into one batch."""
    fids: list[int] = []
    keys: list[str] = []
    seen_keys: set[str] = set()
    by_firing: list[tuple[int, tuple[str, ...]]] = []
    skipped: list[tuple[int, str]] = []
    overflow: list[int] = []

    for firing in candidates:
        fid = int(firing["id"])
        if overflow:
            overflow.append(fid)
            continue
        resolved, skip_reason = recheck.resolve_for_firing(store, defn, firing, when)
        if resolved is None:
            skipped.append((fid, skip_reason))
            continue
        fkeys = tuple(dict.fromkeys(k for k in resolved.get("keys", []) if isinstance(k, str) and k))
        if not fkeys:
            skipped.append((fid, "nothing to do"))
            continue
        dup = astore.same_work_recent(store, defn.action, {"keys": list(fkeys)}, exclude_automation_id=aid, now=when)
        if dup is not None:
            skipped.append((fid, f"already handled by {dup['automation_name']} within the last hour"))
            continue
        new_keys = [k for k in fkeys if k not in seen_keys]
        if fids and len(keys) + len(new_keys) > MERGE_CAP:
            overflow.append(fid)
            continue
        fids.append(fid)
        keys.extend(new_keys)
        seen_keys.update(new_keys)
        by_firing.append((fid, fkeys))

    return MergeBatch(
        firing_ids=tuple(fids),
        keys=tuple(keys),
        key_by_firing=tuple(by_firing),
        skipped=tuple(skipped),
        overflow_ids=tuple(overflow),
    )


def _skip_reasons_by_key(result: Any) -> tuple[dict[str, str], str]:
    by_key: dict[str, str] = {}
    fallback = "skipped by safety checks"
    if not isinstance(result, dict):
        return by_key, fallback
    skipped = result.get("skipped")
    if isinstance(skipped, list):
        for entry in skipped:
            if isinstance(entry, dict) and entry.get("key"):
                reason = str(entry.get("reason") or fallback)[:300]
                by_key[str(entry["key"])] = reason
                if fallback == "skipped by safety checks":
                    fallback = reason
    return by_key, fallback


def _apply_result(store: Store, batch: MergeBatch, result: Any, when: float) -> str:
    by_key, fallback = _skip_reasons_by_key(result)
    all_skipped = isinstance(result, dict) and not result.get("queued") and bool(result.get("skipped"))
    any_done = False
    for fid, fkeys in batch.key_by_firing:
        if all_skipped or all(k in by_key for k in fkeys):
            reason = by_key.get(fkeys[0], fallback) if fkeys else fallback
            astore.set_firing(store, fid, status="skipped", reason=reason, finished=when, now=when)
        else:
            astore.set_firing(store, fid, status="done", finished=when, now=when)
            any_done = True
    return "done" if any_done else "skipped"


def _next_daily_window(store: Store, aid: int, max_per_day: int, when: float) -> float | None:
    """Return the timestamp when `aid`'s rolling 24h call count drops below `max_per_day`, or None if under cap."""
    with store._tx() as db:
        rows = db.execute(
            "SELECT MAX(COALESCE(finished, created)) AS call_ts FROM automation_firings "
            "WHERE automation_id = ? AND status IN ('running', 'done', 'failed') "
            "AND COALESCE(finished, created) >= ? "
            "GROUP BY CASE WHEN trace_id != '' THEN trace_id ELSE CAST(id AS TEXT) END "
            "ORDER BY call_ts DESC",
            (aid, when - 86400.0),
        ).fetchall()
    if len(rows) < max_per_day:
        return None
    return float(rows[max_per_day - 1]["call_ts"]) + 86400.0 + 1.0


def _defer_overflow(
    store: Store, aid: int, defn: AutomationDef, overflow_ids: tuple[int, ...],
    by_id: dict[int, dict[str, Any]], when: float,
) -> tuple[set[int], tuple[int, ...]]:
    """Defer overflow firings past cooldown and daily cap (extending deadline); otherwise return immediate overflow."""
    if not overflow_ids:
        return set(), ()
    next_due = when
    if defn.cooldown_s > 0:
        last = astore.last_fired_at(store, aid)
        if last is not None and when - last < defn.cooldown_s:
            next_due = max(next_due, last + defn.cooldown_s)
    daily_win = _next_daily_window(store, aid, defn.max_per_day, when)
    if daily_win is not None:
        next_due = max(next_due, daily_win)
    if next_due <= when:
        return set(), overflow_ids
    min_deadline = next_due + VERDICT_DEADLINE_S
    for ofid in overflow_ids:
        existing = by_id.get(ofid, {}).get("deadline")
        new_dl = max(float(existing), min_deadline) if existing is not None else None
        astore.set_firing(store, ofid, due=next_due, deadline=new_dl, now=when)
    return set(overflow_ids), ()


def run_merged(
    store: Store,
    actions: Actions,
    aid: int,
    defn: AutomationDef,
    lead_firing: dict[str, Any],
    when: float,
    *,
    on_failure: Callable[[int, str, float], None],
) -> tuple[str, set[int], tuple[int, ...]]:
    """Coalesce and execute due firings for `aid` within 1 minute; returns `(outcome, handled_ids, immediate_overflow)`."""
    siblings = collect_due_siblings(store, aid, lead_firing, when)
    by_id = {int(r["id"]): r for r in siblings}
    batch = build_batch(store, defn, aid, siblings, when)
    handled: set[int] = set()
    for sfid, sreason in batch.skipped:
        astore.set_firing(store, sfid, status="skipped", reason=sreason, finished=when, now=when)
        handled.add(sfid)
    if not batch.firing_ids:
        return "skipped", handled, ()
    handled.update(batch.firing_ids)
    ctx = ActionContext(
        actor=f"automation:{aid}", surface="automation",
        firing_id=batch.firing_ids[0], firing_ids=batch.firing_ids, now=when,
    )
    for fid, fkeys in batch.key_by_firing:
        astore.set_firing(
            store, fid, status="running", input_data={"keys": list(fkeys)},
            trace_id=ctx.trace_id, started_at=when, now=when,
        )
    try:
        result = actions.call(defn.action, {"keys": list(batch.keys)}, ctx)
    except Forbidden as exc:
        for fid in batch.firing_ids:
            astore.set_firing(store, fid, status="refused", reason=exc.message, finished=when, now=when)
        deferred, immediate = _defer_overflow(store, aid, defn, batch.overflow_ids, by_id, when)
        return "skipped", handled | deferred, immediate
    except (ActionError, Exception) as exc:
        for fid in batch.firing_ids:
            astore.set_firing(store, fid, status="failed", reason=str(exc), finished=when, now=when)
        on_failure(aid, defn.name, when)
        deferred, immediate = _defer_overflow(store, aid, defn, batch.overflow_ids, by_id, when)
        return "failed", handled | deferred, immediate
    outcome = _apply_result(store, batch, result, when)
    deferred, immediate = _defer_overflow(store, aid, defn, batch.overflow_ids, by_id, when)
    return outcome, handled | deferred, immediate
