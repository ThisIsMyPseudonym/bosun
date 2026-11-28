"""Verdict trigger scanning, per-rule cursor, untrusted-text isolation and failure context extraction."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import sqlite3
from typing import Any

from pydantic import ValidationError

from .. import recipes
from ..store import Store
from ..triage import check as tcheck, store as tstore
from ..triage.schema import FailureRecord, Verdict
from . import store as astore
from .schema import UNSET_PROJECT, AutomationDef, VerdictConditions

log = logging.getLogger(__name__)

VERDICT_CURSOR = "verdict"
VERDICT_STALENESS_S = 6 * 3600.0
VERDICT_DEADLINE_S = 2 * 3600.0
VERDICT_SCAN_LIMIT = 50
_VALID_SOURCES = frozenset({"rules", "cache", "model"})
_DISPUTED_FEEDBACK = frozenset({"wrong", "disputed"})
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class VerdictEvent:
    """Structured, code-extracted verdict + failure context (never parsed from model prose)."""

    triage_id: int
    failure: str
    trace_id: str
    triage_source: str
    verdict: Verdict
    ended: float
    run_kind: str
    item_key: str
    item_source: str
    project: str
    available: str
    actor: str
    disputed: bool = False


def parse_cursor(raw: str | None, fallback: float) -> tuple[float, int | None]:
    """Parse either a plain float string or composite '<ts>:<triage_id>' cursor."""
    if raw is None:
        return fallback, None
    if ":" in raw:
        ts_part, _, id_part = raw.partition(":")
        try:
            return float(ts_part), int(id_part)
        except ValueError:
            return fallback, None
    try:
        return float(raw), None
    except ValueError:
        return fallback, None


def format_cursor(ended: float, triage_id: int | None) -> str:
    return repr(ended) if triage_id is None else f"{ended!r}:{triage_id}"


def after_cursor(ended: float, triage_id: int, cur: tuple[float, int | None]) -> bool:
    cur_ts, cur_id = cur
    if cur_id is None:
        return ended > cur_ts
    return ended > cur_ts or (ended == cur_ts and triage_id > cur_id)


def is_downgraded_verdict(verdict: Verdict, triage_source: str) -> bool:
    """True when a verdict was downgraded/sanitised or still contains unsafe content."""
    if verdict.cause in (tcheck._CUT_NOTE, tcheck._EMPTY_CAUSE_NOTE):
        return True
    if verdict.next_step.why == tcheck._CUT_NOTE:
        return True
    if triage_source in ("model", "cache"):
        if tcheck.sanitize_text(verdict.cause)[1] or tcheck.sanitize_text(verdict.next_step.why)[1]:
            return True
        if any(tcheck.sanitize_text(ev.ref)[1] for ev in verdict.evidence):
            return True
    return False


def _row_disputed(db: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if row["status"] != "done" or row["feedback"] in _DISPUTED_FEEDBACK:
        return True
    if row["same_as"] is not None:
        orig = db.execute("SELECT status, feedback FROM triage WHERE id = ?", (row["same_as"],)).fetchone()
        if orig is not None and (orig["status"] == "disputed" or orig["feedback"] in _DISPUTED_FEEDBACK):
            return True
    if row["source"] == "rules" and row["signature"]:
        hit = db.execute(
            "SELECT 1 FROM triage_disputed_rules WHERE signature = ? LIMIT 1",
            (row["signature"],),
        ).fetchone()
        if hit is not None:
            return True
    return False


def load_failure_by_key(store: Store, failure: str, trace_id: str = "") -> FailureRecord | None:
    """Load the exact v_failures record for `failure` (`job:<id>`, `fix:<id>`, `run:<trace>`)."""
    what, _, what_id = failure.partition(":")
    with store._tx() as db:
        row = db.execute(
            "SELECT * FROM v_failures WHERE what = ? AND id = ? LIMIT 1",
            (what, what_id),
        ).fetchone()
        if row is None and trace_id:
            row = db.execute(
                "SELECT * FROM v_failures WHERE trace_id = ? LIMIT 1",
                (trace_id,),
            ).fetchone()
        return None if row is None else tstore.record_from_row(db, row)


def recorded_failure_target(store: Store, failure: str) -> tuple[bool, str, str]:
    """Return `(found, item_key, project)` strictly from recorded `jobs`/`fixes`/`runs` rows."""
    what, _, what_id = failure.partition(":")
    with store._tx() as db:
        if what == "job" and what_id.isdigit():
            job = db.execute("SELECT item_key FROM jobs WHERE id = ?", (int(what_id),)).fetchone()
            if job is None:
                return False, "", ""
            key = str(job["item_key"] or "")
            snap = db.execute(
                "SELECT project FROM snapshot_items WHERE item_key = ? ORDER BY snapshot_id DESC LIMIT 1", (key,),
            ).fetchone() if key else None
            return True, key, str(snap["project"] or "") if snap else ""
        if what == "fix" and what_id.isdigit():
            fix = db.execute("SELECT item_key, project FROM fixes WHERE id = ?", (int(what_id),)).fetchone()
            if fix is None:
                return False, "", ""
            return True, str(fix["item_key"] or ""), str(fix["project"] or "")
        if what == "run" and what_id:
            run = db.execute("SELECT kind, subject FROM runs WHERE trace_id = ?", (what_id,)).fetchone()
            if run is None:
                return False, "", ""
            proj = str(run["subject"] or "") if run["kind"] == "recheck-project" else ""
            return True, "", proj
    return False, "", ""


def _recorded_target(db: sqlite3.Connection, rec: FailureRecord) -> tuple[str, str, str]:
    """Extract (item_key, project, available) strictly from recorded DB tables, never verdict prose."""
    item_key = ""
    project = ""
    if rec.what == "job" and rec.what_id.isdigit():
        job = db.execute("SELECT item_key, started FROM jobs WHERE id = ?", (int(rec.what_id),)).fetchone()
        if job is not None:
            item_key = str(job["item_key"] or "")
    elif rec.what == "fix" and rec.what_id.isdigit():
        fix = db.execute("SELECT item_key, project FROM fixes WHERE id = ?", (int(rec.what_id),)).fetchone()
        if fix is not None:
            item_key = str(fix["item_key"] or "")
            project = str(fix["project"] or "")
    elif rec.what == "run" and rec.kind == "recheck-project":
        project = rec.subject
    available = ""
    if item_key:
        snap = db.execute(
            "SELECT si.available, si.project FROM snapshot_items si "
            "JOIN snapshot_log sl ON sl.snapshot_id = si.snapshot_id "
            "WHERE si.item_key = ? AND sl.ts <= ? ORDER BY sl.snapshot_id DESC LIMIT 1",
            (item_key, rec.ts + 5.0),
        ).fetchone()
        if snap is None:
            snap = db.execute(
                "SELECT available, project FROM snapshot_items WHERE item_key = ? ORDER BY snapshot_id DESC LIMIT 1",
                (item_key,),
            ).fetchone()
        if snap is not None:
            available = str(snap["available"] or "")
            project = project or str(snap["project"] or "")
    return item_key, project, available


def _parse_verdict(raw: str | None) -> Verdict | None:
    if not raw:
        return None
    try:
        return Verdict.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None


def build_event(store: Store, row: sqlite3.Row, *, check_disputed: bool = True) -> VerdictEvent | None:
    """Build a VerdictEvent from one `triage` row and its recorded `v_failures` row."""
    src = str(row["source"] or "")
    if src not in _VALID_SOURCES:
        return None
    verdict = _parse_verdict(row["verdict"])
    if verdict is None or is_downgraded_verdict(verdict, src):
        return None
    with store._tx() as db:
        disputed = _row_disputed(db, row)
        if check_disputed and disputed:
            return None
    rec = load_failure_by_key(store, str(row["failure"]), str(row["trace_id"] or ""))
    if rec is None:
        return None
    with store._tx() as db:
        item_key, project, available = _recorded_target(db, rec)
    item_source = item_key.split(":", 1)[0] if ":" in item_key else rec.item_source
    ended = float(row["ended"] if "ended" in row.keys() else (row["finished"] or row["created"] or 0.0))
    return VerdictEvent(
        triage_id=int(row["id"]),
        failure=str(row["failure"]),
        trace_id=rec.trace_id or str(row["trace_id"] or ""),
        triage_source=src,
        verdict=verdict,
        ended=ended,
        run_kind=rec.kind,
        item_key=item_key,
        item_source=item_source,
        project=project,
        available=available,
        actor=rec.actor,
        disputed=disputed,
    )


def matches_verdict(conditions: VerdictConditions, event: VerdictEvent) -> bool:
    """Match ONLY code-produced structured verdict/failure fields; never cause, why, or evidence text."""
    if event.disputed or event.triage_source not in _VALID_SOURCES:
        return False
    if is_downgraded_verdict(event.verdict, event.triage_source):
        return False
    if conditions.project == UNSET_PROJECT:
        return False
    v = event.verdict
    if conditions.categories and v.category not in conditions.categories:
        return False
    if conditions.transient is not None and v.transient is not conditions.transient:
        return False
    if conditions.needs_person is not None and v.needs_person is not conditions.needs_person:
        return False
    if conditions.min_confidence is not None and _CONF_RANK[v.confidence] < _CONF_RANK[conditions.min_confidence]:
        return False
    if conditions.next_step_kind is not None and v.next_step.kind != conditions.next_step_kind:
        return False
    if conditions.next_step_recipe is not None:
        if v.next_step.recipe != conditions.next_step_recipe or conditions.next_step_recipe not in recipes.RECIPES:
            return False
    if conditions.next_step_runbook is not None:
        if v.next_step.runbook != conditions.next_step_runbook or not tcheck._runbook_exists(conditions.next_step_runbook):
            return False
    if conditions.run_kind is not None and event.run_kind != conditions.run_kind:
        return False
    if conditions.source is not None and event.item_source != conditions.source:
        return False
    if conditions.project is not None and event.project != conditions.project:
        return False
    if conditions.verdict_source is not None and event.triage_source != conditions.verdict_source:
        return False
    return True


def _rule_cursors(
    store: Store, verdict_rules: list[tuple[int, dict[str, Any], AutomationDef]], when: float,
) -> dict[int, tuple[float, int | None]]:
    floor = when - VERDICT_STALENESS_S
    cursors: dict[int, tuple[float, int | None]] = {}
    for aid, row, _defn in verdict_rules:
        cur_ts, cur_id = parse_cursor(astore.cursor(store, f"{VERDICT_CURSOR}_{aid}"), float(row.get("updated") or 0.0))
        if cur_ts < floor:
            cursors[aid] = (floor, None)
        else:
            cursors[aid] = (cur_ts, cur_id)
    return cursors


def _fetch_verdict_rows(
    store: Store, since_by_rule: dict[int, tuple[float, int | None]],
) -> list[sqlite3.Row]:
    min_ts = min(ts for ts, _ in since_by_rule.values())
    at_min_ids = [cid for ts, cid in since_by_rule.values() if ts == min_ts and cid is not None]
    base_sql = (
        "SELECT id, failure, trace_id, signature, status, source, verdict, same_as, feedback, "
        "COALESCE(finished, created) AS ended FROM triage "
        "WHERE status = 'done' AND source IN ('rules', 'cache', 'model') "
        "AND feedback NOT IN ('wrong', 'disputed') "
    )
    with store._tx() as db:
        if at_min_ids:
            return db.execute(
                f"{base_sql}AND (COALESCE(finished, created) > ? OR (COALESCE(finished, created) = ? AND id > ?)) "
                "ORDER BY ended ASC, id ASC LIMIT ?",
                (min_ts, min_ts, min(at_min_ids), VERDICT_SCAN_LIMIT),
            ).fetchall()
        return db.execute(
            f"{base_sql}AND COALESCE(finished, created) > ? ORDER BY ended ASC, id ASC LIMIT ?",
            (min_ts, VERDICT_SCAN_LIMIT),
        ).fetchall()


def scan_verdicts(
    store: Store, rules: dict[int, tuple[dict[str, Any], AutomationDef]], when: float,
) -> int:
    """Scan newly stored `done` triage verdicts after each rule's cursor and schedule matching firings."""
    verdict_rules = [(aid, r, d) for aid, (r, d) in rules.items() if d.trigger.kind == "verdict"]
    if not verdict_rules:
        return 0
    since_by_rule = _rule_cursors(store, verdict_rules, when)
    rows = _fetch_verdict_rows(store, since_by_rule)
    created = 0
    max_cur: dict[int, tuple[float, int | None]] = dict(since_by_rule)
    for row in rows:
        ended, tid = float(row["ended"]), int(row["id"])
        eligible_rules = [entry for entry in verdict_rules if after_cursor(ended, tid, since_by_rule[entry[0]])]
        if not eligible_rules:
            continue
        event = build_event(store, row)
        for aid, _row, defn in eligible_rules:
            if after_cursor(ended, tid, max_cur[aid]):
                max_cur[aid] = (ended, tid)
            if event is None or not isinstance(defn.conditions, VerdictConditions):
                continue
            if not matches_verdict(defn.conditions, event):
                continue
            due = when + defn.delay_s
            fid = astore.insert_firing(
                store, aid, defn.name, "verdict", event.failure, f"verdict:{event.failure}",
                status="scheduled", depth=0, due=due, deadline=due + VERDICT_DEADLINE_S,
                input_data={
                    "failure": event.failure, "trace_id": event.trace_id,
                    "item_key": event.item_key, "available": event.available, "project": event.project,
                },
                now=when,
            )
            if fid is not None:
                created += 1
    for aid, cur in max_cur.items():
        if cur != since_by_rule[aid]:
            astore.set_cursor(store, f"{VERDICT_CURSOR}_{aid}", format_cursor(*cur))
    return created
