"""Fast rules-and-cache triage pipeline (Phase 1: no model calls)."""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from bosun import redact, serialize
from bosun.models import Item
from bosun.store import Store
from bosun.triage import check, rules, signature
from bosun.triage import store as tstore
from bosun.triage.schema import FailureRecord, Verdict

SKIPPED_STATUSES: frozenset[str] = frozenset({"interrupted", "cancelled", "no_changes"})


def signature_of(rec: FailureRecord) -> str:
    """Compute the normalized SHA-256 failure signature for a record."""
    return signature.signature(rec)


def _lookup_item(store: Store, subject: str) -> Item | None:
    if not subject or ":" not in subject:
        return None
    snap = store.latest_snapshot()
    if not snap:
        return None
    for raw in snap.get("items", ()):
        if raw.get("key") == subject:
            return serialize.item_from_dict(raw)
    return None


def _recheck(verdict: Verdict, item: Item | None, rec: FailureRecord) -> Verdict:
    step = check.recheck_step(
        verdict.next_step,
        category=verdict.category,
        item=item,
        output=rec.output or rec.error,
        run_kind=rec.kind,
    )
    return verdict.model_copy(update={"next_step": step})


def _ephemeral_row(
    rec: FailureRecord,
    sig: str,
    status: str,
    *,
    source: str = "",
    verdict: Verdict | None = None,
    same_as: int | None = None,
    same_as_run: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    ts = time.time() if now is None else now
    row: dict[str, Any] = {
        "id": None,
        "failure": rec.failure,
        "trace_id": rec.trace_id,
        "signature": sig,
        "status": status,
        "source": source,
        "verdict": redact.value(verdict.model_dump(mode="json")) if verdict is not None else None,
        "same_as": same_as,
        "triage_trace": "",
        "feedback": "",
        "feedback_ts": None,
        "created": ts,
        "finished": ts if status in ("done", "skipped", "failed", "fixed_since") else None,
    }
    if same_as_run is not None:
        row["same_as_run"] = same_as_run
    return row


def _store_or_return(
    store: Store,
    rec: FailureRecord,
    sig: str,
    source: str,
    verdict: Verdict,
    *,
    persist: bool,
    same_as: int | None = None,
    same_as_run: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    if not persist:
        return _ephemeral_row(
            rec, sig, "done", source=source, verdict=verdict,
            same_as=same_as, same_as_run=same_as_run, now=now,
        )
    tstore.save_verdict(store, rec.failure, rec.trace_id, sig, source, verdict, same_as=same_as, now=now)
    return tstore.get(store, rec.failure)


def _reuse_cached(
    store: Store,
    rec: FailureRecord,
    sig: str,
    item: Item | None,
    *,
    persist: bool,
    now: float | None,
) -> dict[str, Any] | None:
    cached = tstore.find_cached(store, sig, now=now)
    if cached is None or not cached.get("verdict"):
        return None
    try:
        validated = Verdict.model_validate(cached["verdict"])
    except ValidationError:
        return None
    reused = _recheck(validated, item, rec)
    return _store_or_return(
        store, rec, sig, "cache", reused, persist=persist,
        same_as=int(cached["id"]), same_as_run=cached.get("same_as_run"), now=now,
    )


def evaluate_fast(
    store: Store,
    rec: FailureRecord,
    *,
    now: float | None = None,
    save: bool = True,
) -> dict[str, Any] | None:
    """Run steps 1-4 (superseded -> signature -> rules -> 30-day cache) without calling a model."""
    if rec.status in SKIPPED_STATUSES:
        return None

    existing = tstore.get(store, rec.failure)
    if existing is not None and (
        existing.get("status") == "disputed" or existing.get("feedback") in ("wrong", "disputed")
    ):
        return existing

    persist = save and not store.read_only
    sig = signature_of(rec)

    if tstore.superseded(store, rec):
        if not persist:
            return _ephemeral_row(rec, sig, "fixed_since", now=now)
        tstore.save_status(store, rec.failure, rec.trace_id, sig, "fixed_since", now=now)
        return tstore.get(store, rec.failure)

    item = _lookup_item(store, rec.subject)
    if not tstore.is_rule_disputed(store, sig):
        matched = rules.match(rec)
        if matched is not None:
            checked = _recheck(matched, item, rec)
            return _store_or_return(store, rec, sig, "rules", checked, persist=persist, now=now)

    return _reuse_cached(store, rec, sig, item, persist=persist, now=now)


def triage_for(
    store: Store,
    trace_id: str,
    *,
    save: bool = True,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return an existing triage row for ``trace_id``, or evaluate rules and cache on read."""
    existing = tstore.get_by_trace(store, trace_id)
    if existing is not None:
        return existing
    rec = tstore.load_failure(store, trace_id)
    if rec is None:
        return None
    return evaluate_fast(store, rec, now=now, save=save)
