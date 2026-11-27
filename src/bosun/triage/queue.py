"""Queue persistence and signature deduplication helpers for automatic background triage."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from bosun import redact
from bosun.store import Store
from bosun.triage import settings as tsettings
from bosun.triage import store as tstore
from bosun.triage.schema import FailureRecord, Verdict

UNDATED_RETRY_S = 3600.0
SCAN_LOOKBACK_S = 3600.0


def scan_failures_with_ts(
    store: Store,
    since: float,
    *,
    lookback_s: float = SCAN_LOOKBACK_S,
) -> list[tuple[FailureRecord, float]]:
    """Read v_failures rows with finish time >= since - lookback_s, oldest finish first."""
    cutoff = max(0.0, since - max(0.0, lookback_s))
    with store._tx() as db:
        rows = db.execute(
            "SELECT v.*, MAX(COALESCE(v.ts, 0.0), COALESCE(r.finished, v.ts, 0.0)) AS event_ts "
            "FROM v_failures v LEFT JOIN runs r ON r.trace_id = v.trace_id "
            "WHERE MAX(COALESCE(v.ts, 0.0), COALESCE(r.finished, v.ts, 0.0)) >= ? "
            "ORDER BY event_ts ASC, v.ts ASC",
            (cutoff,),
        ).fetchall()
        return [(tstore.record_from_row(db, row), float(row["event_ts"] or 0.0)) for row in rows]


def scan_failures(store: Store, since: float) -> list[FailureRecord]:
    """Read and normalize all v_failures rows finishing within the scan window, oldest first."""
    return [rec for rec, _ in scan_failures_with_ts(store, since)]


def rows_by_status(store: Store, status: str) -> list[dict[str, Any]]:
    """Return raw triage metadata rows with the given status, oldest first."""
    with store._tx() as db:
        rows = db.execute(
            "SELECT id, failure, trace_id, signature, status, created FROM triage "
            "WHERE status = ? ORDER BY created ASC, id ASC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_active_signature(store: Store, sig: str, *, exclude_failure: str = "") -> bool:
    """True when another queued, running, or waiting_quota row already holds this signature."""
    if not sig:
        return False
    with store._tx() as db:
        hit = db.execute(
            "SELECT 1 FROM triage WHERE signature = ? "
            "AND status IN ('queued', 'running', 'waiting_quota') AND failure != ? LIMIT 1",
            (sig, exclude_failure),
        ).fetchone()
    return hit is not None


def pending_siblings(store: Store, sig: str, exclude_failure: str) -> list[dict[str, Any]]:
    """Return queued or waiting_quota rows sharing signature ``sig`` other than ``exclude_failure``."""
    if not sig:
        return []
    with store._tx() as db:
        rows = db.execute(
            "SELECT id, failure, trace_id, signature, status FROM triage "
            "WHERE signature = ? AND status IN ('queued', 'waiting_quota') AND failure != ? ORDER BY id ASC",
            (sig, exclude_failure),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_siblings(
    store: Store,
    sig: str,
    origin_id: int,
    verdict: Verdict,
    *,
    exclude_failure: str,
    recheck_fn: Callable[[Verdict, FailureRecord], Verdict],
    now: float | None = None,
) -> int:
    """Fill pending siblings of ``sig`` as cached copies of ``origin_id``, re-checking each item's next step."""
    count = 0
    for sib in pending_siblings(store, sig, exclude_failure):
        rec = tstore.load_failure(store, str(sib["trace_id"]))
        checked = recheck_fn(verdict, rec) if rec is not None else verdict
        tstore.save_verdict(
            store, str(sib["failure"]), str(sib["trace_id"]), sig, "cache", checked,
            same_as=origin_id, now=now,
        )
        tsettings.clear_quota_reset(store, str(sib["failure"]))
        count += 1
    return count


def save_waiting_quota(
    store: Store,
    failure: str,
    trace_id: str,
    sig: str,
    reset_at: float,
    reason: str,
    *,
    triage_trace: str = "",
    now: float | None = None,
) -> None:
    """Mark one failure waiting_quota and persist its earliest reset timestamp."""
    ts = time.time() if now is None else now
    note = redact.text(reason).strip()[:300]
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, triage_trace, last_error, created, finished) "
            "VALUES (?, ?, ?, 'waiting_quota', '', ?, ?, ?, NULL) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "trace_id=excluded.trace_id, signature=excluded.signature, "
            "status=CASE WHEN triage.status='disputed' OR triage.feedback IN ('wrong','disputed') "
            "THEN 'disputed' ELSE 'waiting_quota' END, "
            "triage_trace=CASE WHEN excluded.triage_trace!='' THEN excluded.triage_trace ELSE triage.triage_trace END, "
            "last_error=excluded.last_error, finished=NULL",
            (failure, trace_id, sig, triage_trace, note, ts),
        )
    tsettings.set_quota_reset(store, failure, reset_at)


def mark_siblings_waiting(
    store: Store,
    sig: str,
    exclude_failure: str,
    reset_at: float,
    reason: str,
    *,
    now: float | None = None,
) -> int:
    """Move queued siblings of ``sig`` to waiting_quota with the same reset timestamp."""
    count = 0
    for sib in pending_siblings(store, sig, exclude_failure):
        save_waiting_quota(store, str(sib["failure"]), str(sib["trace_id"]), sig, reset_at, reason, now=now)
        count += 1
    return count


def fail_siblings(
    store: Store,
    sig: str,
    exclude_failure: str,
    reason: str,
    *,
    triage_trace: str = "",
    now: float | None = None,
) -> int:
    """Mark pending siblings of ``sig`` failed after the lead model call failed."""
    count = 0
    for sib in pending_siblings(store, sig, exclude_failure):
        tstore.restore_or_fail(
            store, str(sib["failure"]), str(sib["trace_id"]), sig, None, reason,
            triage_trace=triage_trace, now=now,
        )
        tsettings.clear_quota_reset(store, str(sib["failure"]))
        count += 1
    return count


def save_skipped(
    store: Store,
    failure: str,
    trace_id: str,
    sig: str,
    reason: str = "",
    *,
    now: float | None = None,
) -> None:
    """Mark a failure skipped and record an optional redacted explanation."""
    ts = time.time() if now is None else now
    note = redact.text(reason).strip()[:300]
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, last_error, created, finished) "
            "VALUES (?, ?, ?, 'skipped', '', ?, ?, ?) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "trace_id=excluded.trace_id, signature=excluded.signature, "
            "status=CASE WHEN triage.status='disputed' OR triage.feedback IN ('wrong','disputed') "
            "THEN 'disputed' ELSE 'skipped' END, "
            "last_error=excluded.last_error, finished=excluded.finished",
            (failure, trace_id, sig, note, ts, ts),
        )
    tsettings.clear_quota_reset(store, failure)
