"""Asks-first automation offers: creation, bounds, code-built labels, notifications, and 24-hour expiry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import time
from typing import Any

from pydantic import ValidationError

from .. import attention, autopilot, notify, relaunch
from ..registry import SPECS, Risk
from ..store import Store
from . import digest, match as amatch, recheck, store as astore
from .schema import (SYSTEM_ACTIONS, AutomationDef, ScheduleConditions, grant_digest, grant_digest_matches,
                     grant_expired, has_unset_project)

OFFER_TTL_S = 24 * 3600.0
MAX_OFFERS_PER_RULE = 5
MAX_OFFERS_TOTAL = 20
MAX_OFFER_ITEMS = 20
MERGED_REASON_PREFIX = "merged into offer #"


def requires_offer(defn: AutomationDef) -> bool:
    """True when an automation must always ask first (mode='ask' or any Risk.SYSTEM action)."""
    if defn.mode == "ask" or defn.action in SYSTEM_ACTIONS:
        return True
    spec = SPECS.get(defn.action)
    return spec is not None and spec.risk == Risk.SYSTEM


def _parse_json(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else recheck._parse_json(raw if isinstance(raw, str) else None)


def pending_offer_count(store: Store, *, automation_id: int | None = None) -> int:
    """Count currently open (`status='offered'`) offers, globally or for one automation."""
    sql = "SELECT COUNT(*) FROM automation_firings WHERE status = 'offered'"
    args: tuple[Any, ...] = (automation_id,) if automation_id is not None else ()
    if automation_id is not None:
        sql += " AND automation_id = ?"
    with store._tx() as db:
        row = db.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def duplicate_pending_offer(store: Store, action: str, input_data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an existing open offer for the same action and canonical input, if any."""
    canon = astore._canon_json(dict(input_data))
    with store._tx() as db:
        row = db.execute(
            "SELECT f.* FROM automation_firings f JOIN automations a ON a.id = f.automation_id "
            "WHERE a.action = ? AND f.status = 'offered' AND f.input = ? ORDER BY f.id ASC LIMIT 1",
            (action, canon),
        ).fetchone()
    return dict(row) if row is not None else None


def merged_siblings(store: Store, automation_id: int, lead_firing_id: int) -> tuple[int, ...]:
    """Return sibling firing ids that were merged into `lead_firing_id`'s offer."""
    marker = f"{MERGED_REASON_PREFIX}{lead_firing_id}"
    with store._tx() as db:
        rows = db.execute(
            "SELECT id FROM automation_firings WHERE automation_id = ? AND status = 'skipped' AND reason = ? "
            "ORDER BY id ASC",
            (automation_id, marker),
        ).fetchall()
    return tuple(int(r["id"]) for r in rows)


def _clean_ver(raw: Any) -> str:
    return digest._clean_name(str(raw or ""), limit=30)


def _item_entry(key: str, item: Mapping[str, Any] | None) -> dict[str, str]:
    if item is None:
        return {"key": key[:200], "name": digest._clean_name(key.split(":", 1)[-1]), "installed": "", "available": ""}
    return {"key": str(item.get("key") or key)[:200], "name": digest._clean_name(str(item.get("name") or key)),
            "installed": _clean_ver(item.get("installed")), "available": _clean_ver(item.get("available"))}


def offer_items(
    store: Store, action: str, input_data: Mapping[str, Any],
    *, snapshot_items: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, str], ...]:
    """Build cleaned, bounded item metadata (name + versions) for an offer card."""
    known = snapshot_items if snapshot_items is not None else amatch._snap_items_by_key(store)
    if action == "relaunch_app" and isinstance(input_data.get("app"), str):
        app_id = input_data["app"]
        app_info = relaunch.APPS.get(app_id)
        if app_info is not None:
            for k in app_info.item_keys:
                if k in known:
                    return (_item_entry(k, known[k]),)
            return ({"key": f"relaunch:{app_id}", "name": digest._clean_name(app_info.name), "installed": "", "available": ""},)
        return ({"key": f"relaunch:{app_id}", "name": digest._clean_name(app_id), "installed": "", "available": ""},)
    if action == "open_app" and isinstance(input_data.get("key"), str):
        k = input_data["key"]
        return (_item_entry(k, known.get(k)),)
    raw_keys = input_data.get("keys")
    if isinstance(raw_keys, list):
        return tuple(_item_entry(k, known.get(k)) for k in raw_keys[:MAX_OFFER_ITEMS] if isinstance(k, str) and k)
    if action == "recheck_project" and isinstance(input_data.get("project"), str):
        proj = input_data["project"]
        return ({"key": f"project:{proj[:180]}", "name": digest._clean_name(proj), "installed": "", "available": ""},)
    return ()


def _format_item_with_ver(item: Mapping[str, str]) -> str:
    name = item.get("name") or item.get("key") or "item"
    inst, avail = item.get("installed") or "", item.get("available") or ""
    if inst and avail:
        return f"{name} ({inst} -> {avail})"
    if avail:
        return f"{name} ({avail})"
    return name


def action_summary(action: str, input_data: Mapping[str, Any], items: Sequence[Mapping[str, str]]) -> str:
    """Code-built plain-English summary of exactly what will run and on what (never model text)."""
    names = digest._format_names([i["name"] for i in items if i.get("name")]) if items else ""
    if action == "relaunch_app":
        label = _format_item_with_ver(items[0]) if items else digest._clean_name(str(input_data.get("app") or "app"))
        return f"Quit and reopen {label} to finish its update"
    if action == "open_app":
        label = _format_item_with_ver(items[0]) if items else digest._clean_name(str(input_data.get("key") or "app"))
        return f"Open {label} so its updater can run"
    if action == "upgrade":
        if len(items) == 1:
            return f"Upgrade {_format_item_with_ver(items[0])}"
        return f"Upgrade {len(items)} items: {names}" if items else "Upgrade selected items"
    if action == "start_fix":
        return f"Run Branch + test on {len(items)} item{'s' if len(items) != 1 else ''}: {names}" if items else "Run Branch + test"
    if action == "pin_mcp":
        return f"Pin {len(items)} MCP server{'s' if len(items) != 1 else ''}: {names}" if items else "Pin MCP servers"
    if action == "explain_item":
        return f"Run 'Will this break?' on {names}" if names else "Run 'Will this break?'"
    if action == "recheck_project":
        proj = names or digest._clean_name(str(input_data.get("project") or "project"))
        return f"Re-check project {proj}"
    if action == "refresh":
        only = input_data.get("only")
        which = ", ".join(digest._clean_name(str(x), 24) for x in only[:5]) if isinstance(only, list) and only else "everything"
        return f"Re-scan {which} (without Autopilot)"
    return f"Run {digest._clean_name(action.replace('_', ' '), 40)}"


def offer_why(firing: Mapping[str, Any]) -> str:
    """Code-built reason why the offer fired (never model prose)."""
    kind = str(firing.get("event_kind") or "")
    ref = digest._clean_name(str(firing.get("event_ref") or ""), limit=80)
    if kind == "refresh":
        return "Triggered after a full refresh"
    if kind == "schedule":
        return f"Scheduled slot {ref}" if ref else "Triggered on schedule"
    if kind == "verdict":
        return f"Failure verdict on {ref}" if ref else "Triggered by a failure verdict"
    if kind == "in_app_update":
        return f"In-app update waiting: {ref}" if ref else "In-app update waiting"
    return "Triggered by automation"


def create_offer(
    store: Store, aid: int, defn: AutomationDef, firing: dict[str, Any], resolved: dict[str, Any], when: float,
    *, sibling_fids: tuple[int, ...] = (),
) -> tuple[str, str]:
    """Transition `firing` into an open 24-hour offer, enforcing bounds and notifying at most once."""
    fid = int(firing["id"])
    if pending_offer_count(store, automation_id=aid) >= MAX_OFFERS_PER_RULE:
        reason = f"pending offer limit reached ({MAX_OFFERS_PER_RULE} per rule)"
        _skip_all(store, (fid, *sibling_fids), reason, when)
        return "skipped", reason
    if pending_offer_count(store) >= MAX_OFFERS_TOTAL:
        reason = f"pending offer limit reached ({MAX_OFFERS_TOTAL} total)"
        _skip_all(store, (fid, *sibling_fids), reason, when)
        return "skipped", reason
    dup = duplicate_pending_offer(store, defn.action, resolved)
    if dup is not None:
        reason = f"already offered by {digest._clean_name(str(dup.get('automation_name') or 'another rule'), 60)}"
        _skip_all(store, (fid, *sibling_fids), reason, when)
        return "skipped", reason

    items = offer_items(store, defn.action, resolved)
    summary = action_summary(defn.action, resolved, items)
    astore.set_firing(
        store, fid, status="offered", due=None, deadline=when + OFFER_TTL_S,
        input_data=resolved, reason=summary, now=when,
    )
    if sibling_fids:
        marker = f"{MERGED_REASON_PREFIX}{fid}"
        for sfid in sibling_fids:
            astore.set_firing(store, sfid, status="skipped", reason=marker, finished=when, now=when)
    if notify.is_on(store):
        title = f"Approval needed: {digest._clean_name(defn.name, 60)}"
        notify.enqueue_automation(store, title, f"{summary}. Open Bosun to approve or decline.", now=when)
    return "offered", summary


def _skip_all(store: Store, fids: Sequence[int], reason: str, when: float) -> None:
    for fid in fids:
        astore.set_firing(store, fid, status="skipped", reason=reason, finished=when, now=when)


def _check_app_item_guards(
    store: Store, item_key: str, when: float, *, expected_version: str = "",
) -> str | None:
    known = amatch._snap_items_by_key(store)
    item = known.get(item_key)
    if item is None:
        return "item no longer in snapshot" if known else None
    if expected_version:
        if str(item.get("installed") or "") == expected_version:
            return "fixed since"
        avail = str(item.get("available") or "")
        if avail and avail != expected_version:
            return "version changed since offer"
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
    return None


def _recheck_verdict_offer(
    store: Store, defn: AutomationDef, firing: Mapping[str, Any], siblings: tuple[int, ...],
    stored_input: dict[str, Any], when: float,
) -> str | None:
    all_rows = [firing, *(r for sfid in siblings if (r := astore.get_firing(store, sfid)) is not None)]
    valid_keys: set[str] = set()
    for frow in all_rows:
        resolved, skip_reason = recheck.recheck_verdict_firing(store, defn, frow, when)
        if resolved is None:
            return skip_reason or "conditions no longer hold"
        if isinstance(resolved.get("keys"), list):
            valid_keys.update(k for k in resolved["keys"] if isinstance(k, str))
        elif resolved != stored_input:
            return "conditions no longer hold"
    if isinstance(stored_input.get("keys"), list):
        wanted = {k for k in stored_input["keys"] if isinstance(k, str)}
        if not wanted or not (wanted <= valid_keys):
            return "fixed since"
    return None


def _recheck_keys_action(
    store: Store, defn: AutomationDef, stored_input: dict[str, Any], when: float,
) -> str | None:
    keys = stored_input.get("keys")
    if not isinstance(keys, list) or not keys:
        return "nothing to do"
    known = amatch._snap_items_by_key(store)
    for k in keys:
        if not isinstance(k, str) or not k:
            return "invalid item key"
        why = recheck._item_skip_reason(store, defn, k, "", when)
        if why is not None:
            return why
        item = known.get(k)
        if item is None:
            return "fixed since"
        if defn.action == "start_fix" and (not amatch._is_fixable(item) or store.active_fix_for(k)):
            return "no longer fixable"
        if defn.action == "pin_mcp" and (
            item.get("source") != "mcp" or item.get("fix_kind") != "pin" or store.active_job_for(k)
        ):
            return "no longer pinnable"
        if defn.action == "explain_item" and not amatch._uncached_explain(store, item):
            return "already assessed"
    return None


def _recheck_system_offer(
    store: Store, defn: AutomationDef, firing: Mapping[str, Any], stored_input: dict[str, Any], when: float,
) -> str | None:
    if defn.action == "relaunch_app":
        app_id = stored_input.get("app")
        if not isinstance(app_id, str) or app_id not in relaunch.APPS:
            return "unknown app"
        known = amatch._snap_items_by_key(store)
        for k in relaunch.APPS[app_id].item_keys:
            if k in known:
                why = _check_app_item_guards(store, k, when)
                if why is not None:
                    return why
        return None
    key = stored_input.get("key")
    if not isinstance(key, str) or not key:
        return "unknown app"
    expected_ver = ""
    if firing.get("event_kind") == "in_app_update" and "@" in str(firing.get("event_ref") or ""):
        expected_ver = str(firing["event_ref"]).rsplit("@", 1)[-1]
    return _check_app_item_guards(store, key, when, expected_version=expected_ver)


def recheck_offer(
    store: Store, row: dict[str, Any] | None, defn: AutomationDef | None, firing: dict[str, Any], when: float,
) -> str | None:
    """Re-verify an open offer; returns None if still valid, or a plain reason why it expired."""
    deadline = firing.get("deadline")
    if deadline is not None and when >= float(deadline):
        return "expired after 24 hours"
    if row is None:
        return "automation deleted"
    if not row["enabled"] or row["paused_reason"]:
        return "automation disabled or paused"
    if defn is None:
        return "invalid automation definition"
    if has_unset_project(defn):
        return "automation has no project selected"
    if not grant_digest_matches(row.get("grant_digest"), grant_digest(defn)):
        return "grant digest does not match current definition"
    if grant_expired(defn.action, defn.mode, row.get("granted_at"), now=when):
        return "90-day INSTALL grant expired"
    stored_input = _parse_json(firing.get("input"))
    aid, fid = int(firing["automation_id"]), int(firing["id"])
    siblings = merged_siblings(store, aid, fid)
    if firing.get("event_kind") == "verdict":
        return _recheck_verdict_offer(store, defn, firing, siblings, stored_input, when)
    if isinstance(defn.conditions, ScheduleConditions) and not amatch.matches_schedule(
        store, defn.conditions, defn.action, now=when,
    ):
        return "conditions no longer hold"
    if defn.action in ("upgrade", "pin_mcp", "start_fix", "explain_item"):
        return _recheck_keys_action(store, defn, stored_input, when)
    if defn.action in SYSTEM_ACTIONS:
        return _recheck_system_offer(store, defn, firing, stored_input, when)
    return None


def expire_offer_tx(store: Store, automation_id: int, firing_id: int, reason: str, when: float) -> bool:
    """Atomically mark an open offer and any merged siblings `expired` in one transaction."""
    marker = f"{MERGED_REASON_PREFIX}{firing_id}"
    with store._tx(write=True) as db:
        cur = db.execute(
            "UPDATE automation_firings SET status='expired', reason=?, finished=? WHERE id=? AND status='offered'",
            (astore._clean_reason(reason), when, firing_id),
        )
        if cur.rowcount != 1:
            return False
        db.execute(
            "UPDATE automation_firings SET status='expired', reason=?, finished=? "
            "WHERE automation_id=? AND status='skipped' AND reason=?",
            (f"offer #{firing_id} expired", when, automation_id, marker),
        )
        return True


def expire_offers(store: Store, when: float) -> int:
    """Expire any open offer past 24 hours or whose rule/conditions/guards no longer hold."""
    expired = 0
    for firing in astore.list_firings(store, status="offered", limit=200):
        fid, aid = int(firing["id"]), int(firing["automation_id"])
        row = astore.get(store, aid)
        try:
            defn = astore.parse_def(row) if row is not None else None
        except (ValueError, ValidationError):
            defn = None
        why = recheck_offer(store, row, defn, firing, when)
        if why is not None:
            reason = why if why.startswith("expired") else f"no longer applies: {why}"
            if expire_offer_tx(store, aid, fid, reason, when):
                expired += 1
    return expired


def serialize_offer(
    store: Store, firing: dict[str, Any], *, automations: Mapping[int, dict[str, Any]] | None = None,
    snapshot_items: Mapping[str, dict[str, Any]] | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Serialize one offer row with code-built plain text for Today and Automations cards."""
    when = time.time() if now is None else now
    aid = int(firing["automation_id"])
    rule = automations.get(aid) if automations is not None else astore.get(store, aid)
    action = str(rule["action"]) if rule is not None else ""
    input_data = _parse_json(firing.get("input"))
    items = offer_items(store, action, input_data, snapshot_items=snapshot_items)
    deadline = firing.get("deadline")
    is_expired = firing.get("status") == "expired" or (deadline is not None and when >= float(deadline))
    label = (action_summary(action, input_data, items) if action
             else digest._clean_name(str(firing.get("reason") or "Run automation"), limit=120))
    return {
        "id": int(firing["id"]), "automation_id": aid,
        "automation_name": digest._clean_name(str(firing.get("automation_name") or f"Automation #{aid}"), limit=80),
        "action": action, "action_label": label, "items": list(items), "input": input_data,
        "why": offer_why(firing), "status": "expired" if is_expired else str(firing.get("status") or "offered"),
        "expired": is_expired, "created": firing.get("created"), "deadline": deadline,
    }
