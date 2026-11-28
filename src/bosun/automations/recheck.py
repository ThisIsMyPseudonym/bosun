"""Due-time condition and state re-checking before an automation firing executes."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from .. import attention, autopilot
from ..store import Store
from ..triage import store as tstore
from . import match as amatch, verdict as averdict
from .schema import AutomationDef, ScheduleConditions, VerdictConditions


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _job_succeeded_after(store: Store, item_key: str, after_ts: float) -> bool:
    if not item_key:
        return False
    with store._tx() as db:
        row = db.execute(
            "SELECT 1 FROM jobs WHERE item_key = ? AND status = 'succeeded' "
            "AND COALESCE(finished, started) > ? LIMIT 1",
            (item_key, after_ts),
        ).fetchone()
    return row is not None


def _item_skip_reason(
    store: Store, defn: AutomationDef, item_key: str, wanted_ver: str, when: float,
) -> str | None:
    """Return a plain-text skip reason if `item_key` is fixed, changed, ignored, snoozed, hands-off, or held."""
    known = amatch._snap_items_by_key(store)
    item = known.get(item_key)
    if item is None:
        return "fixed since"
    if defn.action == "upgrade":
        cur_avail = str(item.get("available") or "")
        if not cur_avail:
            return "fixed since"
        if wanted_ver and cur_avail != wanted_ver:
            return "version changed since failure"
    prefs = store.prefs()
    if prefs.get(item_key, {}).get("ignored") or (
        item.get("project") and prefs.get(f"{attention.PROJECT}{item['project']}", {}).get("ignored")
    ):
        return "item is ignored"
    if not attention.autopilot_may(item, prefs, when):
        return "item is snoozed or ignored"
    skip = autopilot.hands_off(store, list(known.values()))
    if item_key in skip:
        return f"left to you: {skip[item_key]}"
    if amatch._item_held(item, when):
        return "held by waiting period"
    if defn.action == "upgrade" and not autopilot.qualifies(item, prefs, skip):
        return "does not qualify for unattended upgrade"
    return None


def recheck_verdict_firing(
    store: Store, defn: AutomationDef, firing: Mapping[str, Any], when: float,
) -> tuple[dict[str, Any] | None, str]:
    """Re-verify verdict status, superseded state, conditions, and item guards when a delayed firing comes due."""
    failure = str(firing.get("event_ref") or "")
    stored_input = _parse_json(firing.get("input"))
    with store._tx() as db:
        row = db.execute(
            "SELECT id, failure, trace_id, signature, status, source, verdict, same_as, feedback, "
            "COALESCE(finished, created) AS ended FROM triage WHERE failure = ? LIMIT 1",
            (failure,),
        ).fetchone()
        if row is None:
            return None, "verdict no longer present"
        if averdict._row_disputed(db, row):
            return None, "verdict disputed"
    verdict = averdict._parse_verdict(row["verdict"])
    if verdict is None or averdict.is_downgraded_verdict(verdict, str(row["source"] or "")):
        return None, "verdict downgraded"
    rec = averdict.load_failure_by_key(store, failure, str(row["trace_id"] or ""))
    if rec is None:
        return None, "failure no longer present"
    if tstore.superseded(store, rec):
        return None, "fixed since"
    event = averdict.build_event(store, row)
    if event is None:
        return None, "verdict no longer valid"
    if _job_succeeded_after(store, event.item_key, rec.ts):
        return None, "fixed since"
    if isinstance(defn.conditions, VerdictConditions) and not averdict.matches_verdict(defn.conditions, event):
        return None, "conditions no longer hold"
    wanted_ver = str(stored_input.get("available") or event.available or "")
    if event.item_key and defn.action in ("upgrade", "pin_mcp", "start_fix", "explain_item"):
        why = _item_skip_reason(store, defn, event.item_key, wanted_ver, when)
        if why is not None:
            return None, why
    ev_dict = {
        "failure": event.failure,
        "trace_id": event.trace_id,
        "item_key": event.item_key,
        "available": wanted_ver,
        "project": event.project,
    }
    resolved = amatch.resolve_input(store, defn, event=ev_dict, now=when)
    if resolved is None:
        return None, "nothing to do"
    return resolved, ""


def resolve_for_firing(
    store: Store, defn: AutomationDef, firing: Mapping[str, Any], when: float,
) -> tuple[dict[str, Any] | None, str]:
    """Re-check trigger conditions and resolve action input at due time; returns `(resolved, skip_reason)`."""
    if firing.get("event_kind") == "verdict":
        return recheck_verdict_firing(store, defn, firing, when)
    if isinstance(defn.conditions, ScheduleConditions) and not amatch.matches_schedule(
        store, defn.conditions, defn.action, now=when,
    ):
        return None, "conditions no longer hold"
    resolved = amatch.resolve_input(store, defn, event=_parse_json(firing.get("input")), now=when)
    if resolved is None:
        return None, "nothing to do"
    return resolved, ""
