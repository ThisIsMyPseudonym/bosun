"""Fast rules-and-cache triage pipeline (Phase 1: no model calls)."""

from __future__ import annotations

from typing import Any

from bosun import serialize
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


def evaluate_fast(
    store: Store,
    rec: FailureRecord,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Run steps 1-4 (superseded -> signature -> rules -> 30-day cache) without calling a model.

    Returns the saved triage row dict, or None when the failure is ineligible or needs a model.
    """
    if rec.status in SKIPPED_STATUSES:
        return None

    sig = signature_of(rec)

    # Step 1: Superseded by a later successful run of the same kind and subject
    if tstore.superseded(store, rec):
        tstore.save_status(store, rec.failure, rec.trace_id, sig, "fixed_since", now=now)
        return tstore.get(store, rec.failure)

    item = _lookup_item(store, rec.subject)

    # Step 3: Deterministic rule table
    matched = rules.match(rec)
    if matched is not None:
        checked = _recheck(matched, item, rec)
        tstore.save_verdict(store, rec.failure, rec.trace_id, sig, "rules", checked, now=now)
        return tstore.get(store, rec.failure)

    # Step 4: 30-day cache of non-disputed model verdicts for the same signature
    cached = tstore.find_cached(store, sig, now=now)
    if cached is not None and cached.get("verdict"):
        reused = _recheck(Verdict.model_validate(cached["verdict"]), item, rec)
        tstore.save_verdict(
            store, rec.failure, rec.trace_id, sig, "cache", reused,
            same_as=int(cached["id"]), now=now,
        )
        return tstore.get(store, rec.failure)

    return None
