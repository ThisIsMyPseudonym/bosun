"""Single-writer 30-second tick engine for Bosun automations."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

from pydantic import ValidationError

from .. import notify
from ..config import Settings
from ..providers.router import QuotaHeld
from ..registry import ActionContext, ActionError, Actions, Forbidden
from ..store import Store
from . import chain, digest, match as amatch, merge as amerge, recheck, schedule, store as astore, verdict as averdict
from .schema import (MODEL_ACTIONS, AutomationDef, RefreshConditions, ScheduleConditions, ScheduleTrigger,
                     grant_digest, grant_digest_matches, grant_expired, has_unset_project, is_install_action)

log = logging.getLogger(__name__)

TICK_S = 30.0
INSTALL_WAIT_BACKOFF_S = 60.0
GLOBAL_DAILY_CAP = 30
SCHEDULE_DEADLINE_S = 6 * 3600.0
REFRESH_STALENESS_S = 6 * 3600.0
REFRESH_DEADLINE_S = 6 * 3600.0
RETRY_DEADLINE_S = 2 * 3600.0
CIRCUIT_BREAKER_FAILURES = 3
MAX_PER_TICK = 10
REFRESH_CURSOR = "refresh"


@dataclass(frozen=True)
class TickReport:
    scanned: int = 0
    ran: int = 0
    skipped: int = 0
    waiting: int = 0
    failed: int = 0
    paused: bool = False
    errors: int = 0


def _skip_summary(skipped: Any) -> str:
    if not isinstance(skipped, list) or not skipped:
        return "skipped by safety checks"
    first = skipped[0]
    if isinstance(first, dict) and first.get("reason"):
        return str(first["reason"])[:300]
    return "skipped by safety checks"


class Automator:
    """Single-writer automation engine running in the dashboard process only."""

    def __init__(self, store: Store, settings: Settings, actions: Actions, *, tz: tzinfo | None = None) -> None:
        self.store = store
        self.settings = settings
        self.actions = actions
        self.tz = tz
        self._tick_lock = threading.Lock()
        self._busy = False

    def busy(self) -> bool:
        return self._busy

    def run(self, interval: float = TICK_S) -> None:
        """Daemon thread loop; never exits on a single bad tick."""
        while True:
            time.sleep(interval)
            self.tick()

    def tick(self, *, now: float | None = None) -> TickReport:
        """Scan triggers and execute due firings (bounded, single-writer, never re-raises)."""
        if self.settings.demo or self.store.read_only:
            return TickReport()
        if not self._tick_lock.acquire(blocking=False):
            return TickReport()
        self._busy = True
        try:
            return self._tick_locked(time.time() if now is None else now)
        except Exception:
            log.exception("automation tick failed")
            return TickReport(errors=1)
        finally:
            self._busy = False
            self._tick_lock.release()

    def _tick_locked(self, when: float) -> TickReport:
        if astore.is_paused(self.store):
            return TickReport(paused=True)
        rules = self._active_rules()
        scanned = (
            self._scan_refreshes(rules, when)
            + self._scan_schedules(rules, when)
            + averdict.scan_verdicts(self.store, rules, when)
        )
        ran, skipped, waiting, failed = self._execute_due(when)
        return TickReport(scanned=scanned, ran=ran, skipped=skipped, waiting=waiting, failed=failed)

    def _active_rules(self) -> dict[int, tuple[dict[str, Any], AutomationDef]]:
        out: dict[int, tuple[dict[str, Any], AutomationDef]] = {}
        for row in astore.list_all(self.store):
            if not row["enabled"] or row["paused_reason"]:
                continue
            try:
                defn = astore.parse_def(row)
                if not has_unset_project(defn):
                    out[int(row["id"])] = (row, defn)
            except (ValueError, ValidationError):
                log.warning("skipping malformed automation %s", row.get("id"))
        return out

    def _snapshot_for_run(self, started: float, finished: float) -> tuple[int, int | None] | None:
        with self.store._tx() as db:
            row = db.execute(
                "SELECT snapshot_id FROM snapshot_log WHERE ts >= ? AND ts <= ? ORDER BY snapshot_id DESC LIMIT 1",
                (started - 5.0, finished + 5.0),
            ).fetchone()
            if row is None:
                return None
            snap_id = int(row["snapshot_id"])
            prev = db.execute(
                "SELECT MAX(snapshot_id) FROM snapshot_log WHERE snapshot_id < ?",
                (snap_id,),
            ).fetchone()
        prev_id = int(prev[0]) if prev and prev[0] is not None else None
        return snap_id, prev_id

    def _scan_refreshes(self, rules: dict[int, tuple[dict[str, Any], AutomationDef]], when: float) -> int:
        ref_rules = [(aid, r, d) for aid, (r, d) in rules.items() if d.trigger.kind == "refresh"]
        if not ref_rules:
            return 0
        floor = when - REFRESH_STALENESS_S
        since_by_rule = {
            aid: max(float(raw) if (raw := astore.cursor(self.store, f"{REFRESH_CURSOR}_{aid}")) is not None
                     else float(row.get("updated") or 0.0), floor)
            for aid, row, _defn in ref_rules
        }
        with self.store._tx() as db:
            runs = db.execute(
                "SELECT trace_id, actor, started, COALESCE(finished, started) AS ended FROM runs "
                "WHERE kind='refresh' AND subject='everything' AND status IN ('ok', 'partial') "
                "AND COALESCE(finished, started) > ? ORDER BY ended ASC LIMIT 20",
                (min(since_by_rule.values()),),
            ).fetchall()
        created, max_ended = 0, dict(since_by_rule)
        for run in runs:
            ended = float(run["ended"])
            snaps = self._snapshot_for_run(float(run["started"]), ended)
            origin = chain.resolve_origin(self.store, str(run["trace_id"] or ""), actor=str(run["actor"] or ""))
            for aid, _row, defn in ref_rules:
                if ended <= since_by_rule[aid]:
                    continue
                max_ended[aid] = max(max_ended[aid], ended)
                if snaps is None or not isinstance(defn.conditions, RefreshConditions):
                    continue
                snap_id, prev_id = snaps
                if not amatch.matches_refresh(self.store, defn.conditions, snapshot_id=snap_id, prev_snapshot_id=prev_id):
                    continue
                depth = chain.next_depth(origin, aid)
                if depth is None:
                    continue
                due = when + defn.delay_s
                fid = astore.insert_firing(
                    self.store, aid, defn.name, "refresh", f"snap:{snap_id}", f"refresh:snap:{snap_id}",
                    status="scheduled", depth=depth, due=due, deadline=due + REFRESH_DEADLINE_S,
                    input_data={"snapshot_id": snap_id, "prev_snapshot_id": prev_id}, now=when,
                )
                created += int(fid is not None)
        for aid, ended in max_ended.items():
            if ended > since_by_rule[aid]:
                astore.set_cursor(self.store, f"{REFRESH_CURSOR}_{aid}", repr(ended))
        return created

    def _scan_schedules(self, rules: dict[int, tuple[dict[str, Any], AutomationDef]], when: float) -> int:
        created = 0
        for aid, _row, defn in ((a, r, d) for a, (r, d) in rules.items() if isinstance(d.trigger, ScheduleTrigger)):
            assert isinstance(defn.trigger, ScheduleTrigger)
            if not isinstance(defn.conditions, ScheduleConditions):
                continue
            seen_slot = astore.cursor(self.store, f"sched_{aid}") or ""
            for slot in schedule.due_slots(defn.trigger, now=when, tz=self.tz):
                dedup = f"schedule:{slot.slot_key}"
                matched = amatch.matches_schedule(self.store, defn.conditions, defn.action, now=when)
                if not slot.missed:
                    if slot.slot_key > seen_slot:
                        astore.set_cursor(self.store, f"sched_{aid}", slot.slot_key)
                        seen_slot = slot.slot_key
                    fid = (astore.insert_firing(
                        self.store, aid, defn.name, "schedule", slot.slot_key, dedup,
                        status="scheduled", due=when + defn.delay_s,
                        deadline=slot.scheduled_ts + SCHEDULE_DEADLINE_S, now=when,
                    ) if matched else None)
                elif matched and slot.slot_key > seen_slot:
                    fid = astore.insert_firing(
                        self.store, aid, defn.name, "schedule", slot.slot_key, dedup,
                        status="missed", reason="missed schedule slot (>2 h late)", now=when,
                    )
                else:
                    fid = None
                if fid is not None:
                    created += 1
        return created

    def _expire_past_deadline(self, when: float) -> int:
        with self.store._tx(write=True) as db:
            cur = db.execute(
                "UPDATE automation_firings SET status='skipped', reason='past deadline', finished=? "
                "WHERE status IN ('scheduled', 'waiting_quota') AND deadline IS NOT NULL AND deadline <= ?",
                (when, when),
            )
            return cur.rowcount

    def _precheck_limits(
        self, aid: int, row: dict[str, Any], defn: AutomationDef, when: float,
    ) -> tuple[str, str] | None:
        """Return `(status, reason)` to stop this firing before execution, or None to proceed."""
        if has_unset_project(defn):
            return ("refused", "automation has no project selected")
        if not grant_digest_matches(row["grant_digest"], grant_digest(defn)):
            return ("refused", "grant digest does not match current definition")
        if grant_expired(defn.action, defn.mode, row["granted_at"], now=when):
            return ("refused", "90-day INSTALL grant expired; renew in the dashboard")
        if astore.global_fired_today(self.store, now=when) >= GLOBAL_DAILY_CAP:
            return ("skipped", f"global daily limit reached ({GLOBAL_DAILY_CAP}/day)")
        if astore.fired_today(self.store, aid, now=when) >= defn.max_per_day:
            return ("skipped", f"daily limit reached ({defn.max_per_day}/day)")
        if defn.cooldown_s > 0:
            last = astore.last_fired_at(self.store, aid)
            if last is not None and when - last < defn.cooldown_s:
                return ("skipped", f"cooldown active ({defn.cooldown_s} s)")
        if defn.action in MODEL_ACTIONS and astore.model_jobs_today(self.store, aid, now=when) >= defn.model_jobs_per_day:
            return ("skipped", f"daily model job limit reached ({defn.model_jobs_per_day}/day)")
        return None

    def _execute_due(self, when: float) -> tuple[int, int, int, int]:
        ran = waiting = failed = 0
        skipped = self._expire_past_deadline(when)
        queue = list(astore.due_firings(self.store, now=when, limit=MAX_PER_TICK))
        handled: set[int] = set()
        steps = 0
        while queue and steps < MAX_PER_TICK:
            firing = queue.pop(0)
            fid = int(firing["id"])
            if fid in handled:
                continue
            if astore.is_paused(self.store):
                break
            steps += 1
            outcome, done_ids, overflow = self._run_isolated(firing, when)
            handled.update(done_ids)
            handled.add(fid)
            if overflow and steps < MAX_PER_TICK:
                nxt = astore.get_firing(self.store, overflow[0])
                if nxt is not None:
                    queue.append(nxt)
            if outcome == "done":
                ran += 1
            elif outcome == "skipped":
                skipped += 1
            elif outcome == "waiting":
                waiting += 1
            elif outcome == "failed":
                failed += 1
        return ran, skipped, waiting, failed

    def _run_isolated(self, firing: dict[str, Any], when: float) -> tuple[str, set[int], tuple[int, ...]]:
        try:
            return self._run_one(firing, when)
        except Exception as exc:
            fid = int(firing["id"])
            log.exception("automation firing %s failed unexpectedly", fid)
            astore.set_firing(
                self.store, fid, status="failed", reason=f"{exc.__class__.__name__}: {exc}",
                finished=when, now=when,
            )
            aid = int(firing["automation_id"])
            try:
                self._check_circuit_breaker(aid, str(firing.get("automation_name") or ""), when)
            except Exception:
                log.exception("circuit breaker check failed for automation %s", aid)
            return "failed", {fid}, ()

    def _run_one(self, firing: dict[str, Any], when: float) -> tuple[str, set[int], tuple[int, ...]]:
        fid, aid = int(firing["id"]), int(firing["automation_id"])
        row = astore.get(self.store, aid)
        if row is None or not row["enabled"] or row["paused_reason"]:
            astore.set_firing(self.store, fid, status="skipped", reason="automation disabled or paused", now=when)
            return "skipped", {fid}, ()
        try:
            defn = astore.parse_def(row)
        except (ValueError, ValidationError):
            astore.set_firing(self.store, fid, status="refused", reason="invalid automation definition", now=when)
            return "skipped", {fid}, ()
        stop = self._precheck_limits(aid, row, defn, when)
        if stop is not None:
            astore.set_firing(self.store, fid, status=stop[0], reason=stop[1], now=when)
            return "skipped", {fid}, ()
        if is_install_action(defn.action) and astore.active_install_firing(self.store) is not None:
            astore.set_firing(self.store, fid, due=when + INSTALL_WAIT_BACKOFF_S, now=when)
            return "waiting", {fid}, ()
        if defn.action in amerge.MERGEABLE_ACTIONS:
            return amerge.run_merged(
                self.store, self.actions, aid, defn, firing, when, on_failure=self._check_circuit_breaker,
            )
        resolved, skip_reason = recheck.resolve_for_firing(self.store, defn, firing, when)
        if resolved is None:
            astore.set_firing(self.store, fid, status="skipped", reason=skip_reason, now=when)
            return "skipped", {fid}, ()
        dup = astore.same_work_recent(self.store, defn.action, resolved, exclude_automation_id=aid, now=when)
        if dup is not None:
            astore.set_firing(
                self.store, fid, status="skipped",
                reason=f"already handled by {dup['automation_name']} within the last hour", now=when,
            )
            return "skipped", {fid}, ()
        if defn.action == "notify":
            return self._run_notify(fid, aid, defn, resolved, when), {fid}, ()
        return self._run_action(firing, aid, defn, resolved, when), {fid}, ()

    def _run_notify(self, fid: int, aid: int, defn: AutomationDef, resolved: dict[str, Any], when: float) -> str:
        ctx = ActionContext(actor=f"automation:{aid}", surface="automation", firing_id=fid, now=when)
        try:
            self.actions.permit_automation_notify(ctx)
        except Forbidden as exc:
            astore.set_firing(self.store, fid, status="refused", reason=exc.message, finished=when, now=when)
            return "skipped"
        snap_id = resolved.get("snapshot_id")
        if not isinstance(snap_id, int):
            latest = self.store.latest_snapshot()
            snap_id = int(latest["id"]) if latest else None
        if snap_id is None:
            astore.set_firing(self.store, fid, status="skipped", reason="no snapshot yet", now=when)
            return "skipped"
        prev_id = resolved.get("prev_snapshot_id")
        astore.set_firing(self.store, fid, status="running", input_data=resolved, started_at=when, now=when)
        line = digest.build_digest(self.store, snapshot_id=snap_id, prev_snapshot_id=prev_id if isinstance(prev_id, int) else None)
        notify.enqueue_automation(self.store, defn.name, line)
        astore.set_firing(self.store, fid, status="done", reason=line, finished=when, now=when)
        return "done"

    def _run_action(
        self, firing: dict[str, Any], aid: int, defn: AutomationDef, resolved: dict[str, Any], when: float,
    ) -> str:
        fid = int(firing["id"])
        ctx = ActionContext(actor=f"automation:{aid}", surface="automation", firing_id=fid, now=when)
        astore.set_firing(
            self.store, fid, status="running", input_data=resolved, trace_id=ctx.trace_id, started_at=when, now=when,
        )
        try:
            result = self.actions.call(defn.action, resolved, ctx)
        except QuotaHeld as exc:
            return self._handle_quota_held(firing, exc, when)
        except Forbidden as exc:
            astore.set_firing(self.store, fid, status="refused", reason=exc.message, finished=when, now=when)
            return "skipped"
        except (ActionError, Exception) as exc:
            astore.set_firing(self.store, fid, status="failed", reason=str(exc), finished=when, now=when)
            self._check_circuit_breaker(aid, defn.name, when)
            return "failed"
        if isinstance(result, dict) and defn.action in ("upgrade", "pin_mcp"):
            if not result.get("queued") and result.get("skipped"):
                astore.set_firing(
                    self.store, fid, status="skipped", reason=_skip_summary(result["skipped"]),
                    finished=when, now=when,
                )
                return "skipped"
        astore.set_firing(self.store, fid, status="done", finished=when, now=when)
        return "done"

    def _handle_quota_held(self, firing: dict[str, Any], exc: QuotaHeld, when: float) -> str:
        fid = int(firing["id"])
        deadline = firing.get("deadline")
        reset = exc.earliest_reset
        if deadline is not None and (when >= deadline or (reset is not None and reset >= deadline)):
            astore.set_firing(
                self.store, fid, status="skipped", reason=f"quota held past deadline: {exc.reason}",
                finished=when, now=when,
            )
            return "skipped"
        next_due = reset if (reset is not None and reset > when) else when + TICK_S
        astore.set_firing(self.store, fid, status="waiting_quota", due=next_due, reason=exc.reason, now=when)
        return "waiting"

    def _check_circuit_breaker(self, aid: int, name: str, when: float) -> None:
        if astore.consecutive_failures(self.store, aid) >= CIRCUIT_BREAKER_FAILURES:
            msg = f"Paused after {CIRCUIT_BREAKER_FAILURES} consecutive failures"
            astore.set_paused_reason(self.store, aid, msg, now=when)
            notify.enqueue_automation(self.store, f"Automation paused: {name}", msg)
