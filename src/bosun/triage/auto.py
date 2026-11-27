"""Background automatic triage tick: scan new failures, revisit waiting_quota, and drain bounded model jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

from bosun.registry import Conflict
from bosun.store import Store
from bosun.triage import model as tmodel
from bosun.triage import queue as tqueue
from bosun.triage import settings as tsettings
from bosun.triage import store as tstore
from bosun.triage.schema import FailureRecord, Verdict

AUTO_MAX_AGE_S = 86400.0


@dataclass(frozen=True)
class TickReport:
    """Summary counters returned by one Triager.tick() pass."""

    rules_or_cache: int = 0
    queued: int = 0
    model_ran: int = 0
    cache_filled: int = 0
    waiting_quota: int = 0
    skipped: int = 0


def scan_new(
    store: Store,
    when: float,
    *,
    eligible_rules: Callable[[FailureRecord], bool],
    eligible_auto: Callable[[FailureRecord], bool],
    eval_fast: Callable[[FailureRecord, float], dict[str, Any] | None],
    sig_of: Callable[[FailureRecord], str],
) -> tuple[int, int, int]:
    """Scan v_failures after cursor; apply rules/cache now and queue unplaced eligible failures."""
    since = tsettings.cursor(store)
    records = tqueue.scan_failures(store, since)
    cfg = tsettings.read(store)
    max_ts = since
    fast_n = queued_n = skipped_n = 0
    for rec in records:
        max_ts = max(max_ts, rec.ts)
        if not eligible_rules(rec) or tstore.get(store, rec.failure) is not None:
            continue
        if eval_fast(rec, when) is not None:
            fast_n += 1
            continue
        if not cfg.auto or not eligible_auto(rec):
            continue
        sig = sig_of(rec)
        if when - rec.ts > AUTO_MAX_AGE_S:
            tqueue.save_skipped(store, rec.failure, rec.trace_id, sig, "Failure is older than 24 hours", now=when)
            skipped_n += 1
            continue
        tstore.save_status(store, rec.failure, rec.trace_id, sig, "queued", now=when)
        queued_n += 1
    if max_ts > since:
        tsettings.set_cursor(store, max_ts)
    return fast_n, queued_n, skipped_n


def revisit_waiting(
    store: Store,
    when: float,
    *,
    reuse_cached: Callable[[FailureRecord, str, float], dict[str, Any] | None],
) -> tuple[int, int, int]:
    """Re-check waiting_quota rows: expire past 24h, mark fixed_since, fill cache, or re-queue at reset."""
    resets = tsettings.quota_resets(store)
    cfg = tsettings.read(store)
    fixed_n = cached_n = skipped_n = 0
    for row in tqueue.rows_by_status(store, "waiting_quota"):
        failure, tid, sig = str(row["failure"]), str(row["trace_id"]), str(row["signature"] or "")
        rec = tstore.load_failure(store, tid)
        if rec is None or when - rec.ts > AUTO_MAX_AGE_S:
            tqueue.save_skipped(store, failure, tid, sig, "Expired while waiting for quota reset", now=when)
            skipped_n += 1
            continue
        if tstore.superseded(store, rec):
            tstore.save_status(store, failure, tid, sig, "fixed_since", now=when)
            tsettings.clear_quota_reset(store, failure)
            fixed_n += 1
            continue
        if reuse_cached(rec, sig, when) is not None:
            tsettings.clear_quota_reset(store, failure)
            cached_n += 1
            continue
        reset_at = resets.get(failure)
        if cfg.auto and (reset_at is None or when >= reset_at):
            tstore.save_status(store, failure, tid, sig, "queued", now=when)
            tsettings.clear_quota_reset(store, failure)
    return fixed_n, cached_n, skipped_n


def _mark_rest_waiting(store: Store, reset_at: float, reason: str, when: float) -> int:
    count = 0
    for rest in tqueue.rows_by_status(store, "queued"):
        tqueue.save_waiting_quota(
            store, str(rest["failure"]), str(rest["trace_id"]), str(rest["signature"] or ""),
            reset_at, reason, now=when,
        )
        count += 1
    return count


def _mark_rest_skipped(store: Store, reason: str, when: float) -> int:
    count = 0
    for rest in tqueue.rows_by_status(store, "queued"):
        tqueue.save_skipped(
            store, str(rest["failure"]), str(rest["trace_id"]), str(rest["signature"] or ""),
            reason, now=when,
        )
        count += 1
    return count


def _run_one_auto(
    triager: Any,
    rec: FailureRecord,
    sig: str,
    when: float,
    recheck_fn: Callable[[Verdict, FailureRecord], Verdict],
) -> tuple[str, int, float | None, str]:
    triager._acquire(rec.trace_id)
    try:
        tstore.save_status(triager.store, rec.failure, rec.trace_id, sig, "running", now=when)
        outcome = tmodel.run_model(
            triager.store, triager.settings, rec, clicked=False, tokens=triager.tokens, now=lambda: when,
        )
        if outcome.ok and outcome.verdict is not None:
            origin_id = tstore.save_verdict(
                triager.store, rec.failure, rec.trace_id, sig, "model", outcome.verdict,
                triage_trace=outcome.triage_trace, now=when,
            )
            filled = tqueue.resolve_siblings(
                triager.store, sig, origin_id, outcome.verdict,
                exclude_failure=rec.failure, recheck_fn=recheck_fn, now=when,
            )
            return "done", filled, None, ""
        if outcome.waiting_quota:
            reset_at = outcome.earliest_reset if outcome.earliest_reset is not None else when + tqueue.UNDATED_RETRY_S
            reason = outcome.reason or "Waiting for quota reset"
            tqueue.save_waiting_quota(
                triager.store, rec.failure, rec.trace_id, sig, reset_at, reason,
                triage_trace=outcome.triage_trace, now=when,
            )
            sibs = tqueue.mark_siblings_waiting(triager.store, sig, rec.failure, reset_at, reason, now=when)
            return "waiting_quota", 1 + sibs, reset_at, reason
        if outcome.no_cli:
            tqueue.save_skipped(triager.store, rec.failure, rec.trace_id, sig, outcome.reason, now=when)
            return "no_cli", 1, None, outcome.reason
        reason = outcome.reason or "Triage model call failed"
        tstore.restore_or_fail(
            triager.store, rec.failure, rec.trace_id, sig, None, reason,
            triage_trace=outcome.triage_trace, now=when,
        )
        tqueue.fail_siblings(triager.store, sig, rec.failure, reason, triage_trace=outcome.triage_trace, now=when)
        return "failed", 0, None, reason
    finally:
        triager._release(rec.trace_id)


def _prepare_queued_row(
    store: Store,
    row: dict[str, Any],
    when: float,
    *,
    eligible_auto: Callable[[FailureRecord], bool],
    reuse_cached: Callable[[FailureRecord, str, float], dict[str, Any] | None],
) -> tuple[str, FailureRecord | None]:
    failure, tid, sig = str(row["failure"]), str(row["trace_id"]), str(row["signature"] or "")
    fresh = tstore.get(store, failure)
    if fresh is None or fresh["status"] != "queued":
        return "skip_silent", None
    rec = tstore.load_failure(store, tid)
    if rec is None or not eligible_auto(rec) or when - rec.ts > AUTO_MAX_AGE_S:
        tqueue.save_skipped(store, failure, tid, sig, "Not eligible for automatic triage", now=when)
        return "skipped", None
    if tstore.superseded(store, rec):
        tstore.save_status(store, failure, tid, sig, "fixed_since", now=when)
        return "fixed", None
    if reuse_cached(rec, sig, when) is not None:
        return "cached", None
    return "ready", rec


def drain_queued(
    triager: Any,
    when: float,
    *,
    eligible_auto: Callable[[FailureRecord], bool],
    reuse_cached: Callable[[FailureRecord, str, float], dict[str, Any] | None],
    recheck_fn: Callable[[Verdict, FailureRecord], Verdict],
) -> tuple[int, int, int, int, int]:
    """Run queued background model triage jobs one at a time up to remaining daily budget."""
    store = triager.store
    if not tsettings.read(store).auto:
        return 0, 0, 0, 0, 0
    budget = tsettings.remaining_budget(store, now=when)
    fixed_n = model_n = cache_n = wait_n = skip_n = started = 0
    seen_sigs: set[str] = set()
    for row in tqueue.rows_by_status(store, "queued"):
        status, rec = _prepare_queued_row(store, row, when, eligible_auto=eligible_auto, reuse_cached=reuse_cached)
        if status == "skipped":
            skip_n += 1
            continue
        if status == "fixed":
            fixed_n += 1
            continue
        if status == "cached":
            cache_n += 1
            continue
        if status != "ready" or rec is None:
            continue
        sig = str(row["signature"] or "")
        if sig in seen_sigs or not tsettings.read(store).auto:
            continue
        if started >= budget or tsettings.remaining_budget(store, now=when) <= 0:
            break
        try:
            state, extra, reset_at, reason = _run_one_auto(triager, rec, sig, when, recheck_fn)
        except Conflict:
            continue
        seen_sigs.add(sig)
        if state == "done":
            model_n, cache_n, started = model_n + 1, cache_n + extra, started + 1
        elif state == "waiting_quota":
            wait_n += extra + _mark_rest_waiting(store, reset_at or when + tqueue.UNDATED_RETRY_S, reason, when)
            break
        elif state == "no_cli":
            skip_n += extra + _mark_rest_skipped(store, reason, when)
            break
        else:
            started += 1
    return fixed_n, model_n, cache_n, wait_n, skip_n


def run_tick(
    triager: Any,
    *,
    now: float | None = None,
    eligible_rules: Callable[[FailureRecord], bool],
    eligible_auto: Callable[[FailureRecord], bool],
    eval_fast: Callable[[FailureRecord, float], dict[str, Any] | None],
    reuse_cached: Callable[[FailureRecord, str, float], dict[str, Any] | None],
    sig_of: Callable[[FailureRecord], str],
    recheck_fn: Callable[[Verdict, FailureRecord], Verdict],
) -> TickReport:
    """Execute one full automatic triage tick safely on a writeable store."""
    if triager.store.read_only:
        return TickReport()
    when = time.time() if now is None else now
    with triager._tick_lock:
        fast_n, queued_n, skip_scan = scan_new(
            triager.store, when,
            eligible_rules=eligible_rules, eligible_auto=eligible_auto, eval_fast=eval_fast, sig_of=sig_of,
        )
        fix_wait, cache_wait, skip_wait = revisit_waiting(triager.store, when, reuse_cached=reuse_cached)
        fix_q, model_n, cache_q, wait_n, skip_q = drain_queued(
            triager, when, eligible_auto=eligible_auto, reuse_cached=reuse_cached, recheck_fn=recheck_fn,
        )
    return TickReport(
        rules_or_cache=fast_n + fix_wait + fix_q,
        queued=queued_n,
        model_ran=model_n,
        cache_filled=cache_wait + cache_q,
        waiting_quota=wait_n,
        skipped=skip_scan + skip_wait + skip_q,
    )
