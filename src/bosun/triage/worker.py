"""Error triage pipeline: fast rules/cache evaluation and on-click model triage worker."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from pydantic import ValidationError

from bosun import auth, redact, serialize
from bosun.config import Settings
from bosun.models import Item
from bosun.registry import Conflict, Failed, Invalid, NotFound
from bosun.store import Store
from bosun.triage import auto as tauto
from bosun.triage import check, queue as tqueue, rules, settings as tsettings, signature
from bosun.triage import model as tmodel
from bosun.triage import store as tstore
from bosun.triage.auto import AUTO_MAX_AGE_S, TickReport
from bosun.triage.schema import FailureRecord, Verdict

log = logging.getLogger(__name__)
SKIPPED_STATUSES: frozenset[str] = frozenset({"interrupted", "cancelled", "no_changes"})
AUTO_MODEL_KINDS: frozenset[str] = frozenset({
    "upgrade", "pin", "manual", "fix", "refresh", "recheck", "recheck-project",
})
REFRESH_KINDS: frozenset[str] = frozenset({"refresh", "recheck", "recheck-project"})
MAX_CONCURRENT_TRIAGE = 2
TICK_S = 30.0
__all__ = [
    "AUTO_MAX_AGE_S", "MAX_CONCURRENT_TRIAGE", "SKIPPED_STATUSES", "TICK_S", "TickReport",
    "Triager", "eligible_for_auto_model", "eligible_for_rules", "evaluate_fast", "signature_of", "triage_for",
]


def eligible_for_rules(rec: FailureRecord) -> bool:
    """True when rules and cache evaluation may run for this failure."""
    if rec.status in SKIPPED_STATUSES or rec.kind == "triage":
        return False
    return True


def eligible_for_auto_model(rec: FailureRecord) -> bool:
    """True when automatic background model triage may run if rules and cache don't place this failure."""
    if not eligible_for_rules(rec):
        return False
    if rec.actor.startswith("agent:") or rec.actor == "assistant":
        return False
    if rec.kind not in AUTO_MODEL_KINDS and rec.what not in ("job", "fix"):
        return False
    if rec.kind in REFRESH_KINDS and rec.status == "partial" and rules.is_outage_only(rec.error):
        return False
    return True


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
        "last_error": "",
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
    if not eligible_for_rules(rec):
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


def _aged_out(found: dict[str, Any]) -> bool:
    """True when spans have aged out and no job/fix output remains for the run."""
    if found.get("spans"):
        return False
    if any(j.get("output") for j in found.get("jobs") or ()):
        return False
    if any(f.get("result") for f in found.get("fixes") or ()):
        return False
    return True


class Triager:
    """Coordinates on-click (and later automatic) model triage with per-run and global concurrency caps."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        tokens: auth.TriageTokens,
        *,
        max_concurrent: int = MAX_CONCURRENT_TRIAGE,
    ) -> None:
        self.store = store
        self.settings = settings
        self.tokens = tokens
        self._max_concurrent = max(1, max_concurrent)
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._active_runs: set[str] = set()

    def _acquire(self, tid: str) -> None:
        with self._lock:
            if tid in self._active_runs:
                raise Conflict("Triage is already running for this run")
            if len(self._active_runs) >= self._max_concurrent:
                raise Conflict("Too many triage jobs are already running; try again shortly")
            self._active_runs.add(tid)

    def _release(self, tid: str) -> None:
        with self._lock:
            self._active_runs.discard(tid)

    def _recheck_record(self, verdict: Verdict, rec: FailureRecord) -> Verdict:
        return _recheck(verdict, _lookup_item(self.store, rec.subject), rec)

    def _run_model_click(self, rec: FailureRecord, tid: str) -> dict[str, Any]:
        sig = signature_of(rec)
        previous = tstore.get(self.store, rec.failure)
        try:
            tstore.save_status(self.store, rec.failure, tid, sig, "running", source="click")
            outcome = tmodel.run_model(self.store, self.settings, rec, clicked=True, tokens=self.tokens)
            if outcome.ok and outcome.verdict is not None:
                origin_id = tstore.save_verdict(
                    self.store, rec.failure, tid, sig, "model", outcome.verdict,
                    triage_trace=outcome.triage_trace,
                )
                tsettings.clear_quota_reset(self.store, rec.failure)
                tqueue.resolve_siblings(
                    self.store, sig, origin_id, outcome.verdict,
                    exclude_failure=rec.failure, recheck_fn=self._recheck_record,
                )
                saved = tstore.get(self.store, rec.failure)
                assert saved is not None
                return saved
            reason = outcome.reason or "Triage model call failed"
            tstore.restore_or_fail(
                self.store, rec.failure, tid, sig, previous, reason, triage_trace=outcome.triage_trace,
            )
            raise Failed(reason)
        except Failed:
            raise
        except Exception as exc:
            log.exception("triage click failed unexpectedly for %s", tid[:12])
            reason = f"Triage failed unexpectedly ({exc.__class__.__name__})"
            try:
                tstore.restore_or_fail(self.store, rec.failure, tid, sig, previous, reason)
            except Exception:
                log.exception("failed to restore triage state for %s", tid[:12])
            raise Failed(reason) from None

    def triage_click(self, trace_id: str, *, deep: bool = False) -> dict[str, Any]:
        """Run on-click triage (or Look deeper when deep=True) for one failed run."""
        found = self.store.trace(trace_id)
        if found is None:
            raise NotFound("No run with that id (or the prefix matches more than one)")
        tid = str(found["run"]["trace_id"])
        rec = tstore.load_failure(self.store, tid)
        if rec is None:
            raise Invalid("This run did not fail and does not need triage")
        if rec.status in SKIPPED_STATUSES:
            raise Invalid(f"Runs with status {rec.status!r} cannot be triaged")
        if _aged_out(found):
            raise Conflict("The output for this run has aged out")
        if not deep:
            fast = evaluate_fast(self.store, rec)
            if fast is not None and fast.get("status") in ("done", "fixed_since"):
                return fast
        self._acquire(tid)
        try:
            return self._run_model_click(rec, tid)
        finally:
            self._release(tid)

    def tick(self, *, now: float | None = None) -> TickReport:
        """Scan new failures, evaluate rules/cache immediately, revisit waiting_quota, and run bounded auto model jobs."""
        return tauto.run_tick(
            self,
            now=now,
            eligible_rules=eligible_for_rules,
            eligible_auto=eligible_for_auto_model,
            eval_fast=lambda rec, when: evaluate_fast(self.store, rec, now=when, save=True),
            reuse_cached=lambda rec, sig, when: _reuse_cached(
                self.store, rec, sig, _lookup_item(self.store, rec.subject), persist=True, now=when,
            ),
            sig_of=signature_of,
            recheck_fn=self._recheck_record,
        )

    def run(self, interval: float = TICK_S) -> None:
        """Dedicated single background thread loop for automatic error triage."""
        while True:
            time.sleep(interval)
            try:
                self.tick()
            except Exception:  # a bad tick must not stop future ones
                log.exception("background triage tick failed")
