"""Pure condition matching, scope resolution and `input_in_scope` verification for automations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from .. import attention, autopilot
from ..enrich import explain
from ..registry import FixIn, KeyIn, KeysIn, PinIn, ProjectIn, RefreshIn, RelaunchIn
from ..serialize import item_from_dict
from ..store import Store
from . import store as astore
from .schema import (
    UNSET_PROJECT,
    AppActionScope,
    AutomationDef,
    ExplainItemScope,
    NotifyScope,
    PinMcpScope,
    RecheckProjectScope,
    RefreshConditions,
    RefreshScope,
    ScheduleConditions,
    StartFixScope,
    UpgradeScope,
    has_unset_project,
)

_FIXABLE_SOURCES = frozenset({"repo", "container"})


def _snap_items_by_key(store: Store) -> dict[str, dict[str, Any]]:
    snap = store.latest_snapshot() or {}
    return {str(i["key"]): i for i in snap.get("items", []) if isinstance(i, dict) and "key" in i}


def _snapshot_rows(store: Store, snapshot_id: int | None) -> dict[str, tuple[str, int, int]]:
    if snapshot_id is None:
        return {}
    with store._tx() as db:
        rows = db.execute(
            "SELECT item_key, lane, kev, ignored FROM snapshot_items WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
    return {str(r["item_key"]): (str(r["lane"] or ""), int(r["kev"] or 0), int(r["ignored"] or 0)) for r in rows}


def _snapshot_ts(store: Store, snapshot_id: int | None) -> float:
    if snapshot_id is None:
        return 0.0
    with store._tx() as db:
        row = db.execute("SELECT ts FROM snapshot_log WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    return float(row["ts"]) if row and row["ts"] is not None else 0.0


def _any_failed_between(store: Store, start_ts: float, end_ts: float) -> bool:
    with store._tx() as db:
        row = db.execute(
            "SELECT 1 FROM jobs WHERE status='failed' AND finished IS NOT NULL AND finished > ? AND finished <= ? LIMIT 1",
            (start_ts, end_ts),
        ).fetchone()
    return row is not None


def matches_refresh(
    store: Store, conditions: RefreshConditions, *, snapshot_id: int, prev_snapshot_id: int | None,
) -> bool:
    """Evaluate all configured `refresh` conditions against two consecutive snapshots."""
    if not conditions.new_in_lanes and not conditions.newly_exploited and not conditions.any_failed:
        return True
    curr = _snapshot_rows(store, snapshot_id)
    prev = _snapshot_rows(store, prev_snapshot_id)
    if conditions.new_in_lanes:
        wanted = set(conditions.new_in_lanes)
        has_new = any(
            not ign and lane in wanted and (k not in prev or prev[k][0] != lane)
            for k, (lane, _kev, ign) in curr.items()
        )
        if not has_new:
            return False
    if conditions.newly_exploited:
        has_kev = any(not ign and kev == 1 and (k not in prev or prev[k][1] != 1) for k, (_l, kev, ign) in curr.items())
        if not has_kev:
            return False
    if conditions.any_failed:
        prev_ts = _snapshot_ts(store, prev_snapshot_id)
        curr_ts = _snapshot_ts(store, snapshot_id)
        if not _any_failed_between(store, prev_ts, curr_ts):
            return False
    return True


def _is_fixable(item: dict[str, Any]) -> bool:
    return item.get("source") in _FIXABLE_SOURCES and bool(item.get("available") or item.get("malicious"))


def _uncached_explain(store: Store, item: dict[str, Any]) -> bool:
    return store.cache_get("explain", explain.cache_key(item_from_dict(item))) is None


def _item_held(item: dict[str, Any], now: float | None = None) -> bool:
    if item.get("source") == "mcp" and item.get("fix_kind") == "pin" and not item.get("available") and "offered" not in item:
        return False  # mcp_pin.settled() enforces autopilot.WAIT_DAYS against registry publish times
    return autopilot.held_until(item, now) is not None


def _item_allowed(
    item: dict[str, Any], prefs: dict[str, dict[str, Any]], skip: Mapping[str, str], now: float | None = None,
) -> bool:
    """Autopilot parity guard for automation scope resolution: skip hands-off, ignored, snoozed, and held items."""
    if item["key"] in skip:
        return False
    if not attention.autopilot_may(item, prefs, now):
        return False
    if item.get("project") and prefs.get(f"{attention.PROJECT}{item['project']}", {}).get("ignored"):
        return False
    return not _item_held(item, now)


def matches_schedule(
    store: Store, conditions: ScheduleConditions, action: str, *, now: float | None = None,
) -> bool:
    """Evaluate `schedule` conditions against the latest snapshot for the target action."""
    if conditions.project == UNSET_PROJECT:
        return False
    if conditions.project is None and conditions.lane is None:
        return True
    items = list(_snap_items_by_key(store).values())
    prefs = store.prefs()
    skip = autopilot.hands_off(store, items)
    candidates = [
        i for i in items
        if attention.in_front(i, prefs, now)
        and _item_allowed(i, prefs, skip, now)
        and (conditions.project is None or i.get("project") == conditions.project)
        and (conditions.lane is None or i.get("lane") == conditions.lane)
    ]
    if action == "start_fix":
        return any(_is_fixable(i) and not store.active_fix_for(str(i["key"])) for i in candidates)
    if action == "explain_item":
        return any(_uncached_explain(store, i) for i in candidates)
    if action == "upgrade":
        return any(autopilot.eligible(i, prefs, skip, now) for i in candidates)
    return bool(candidates)


def _resolve_fix_keys(
    store: Store, scope: StartFixScope, event: Mapping[str, Any], now: float | None = None, *, is_verdict: bool = False,
) -> list[str]:
    known = _snap_items_by_key(store)
    prefs, skip = store.prefs(), autopilot.hands_off(store, list(known.values()))
    keys = [
        k for k, i in known.items()
        if i.get("project") == scope.project and i.get("lane") == scope.lane
        and _is_fixable(i) and attention.in_front(i, prefs, now)
        and _item_allowed(i, prefs, skip, now) and not store.active_fix_for(k)
    ][:100]
    if is_verdict and isinstance(event.get("item_key"), str) and event["item_key"]:
        return [event["item_key"]] if event["item_key"] in keys else []
    return keys


def _resolve_explain_keys(
    store: Store, scope: ExplainItemScope, event: Mapping[str, Any], now: float | None = None, *, is_verdict: bool = False,
) -> list[str]:
    known = _snap_items_by_key(store)
    prefs, skip = store.prefs(), autopilot.hands_off(store, list(known.values()))
    if is_verdict and isinstance(event.get("item_key"), str) and event["item_key"]:
        k = event["item_key"]
        i = known.get(k)
        if (i is not None and i.get("project") == scope.project and i.get("lane") == scope.lane
                and attention.in_front(i, prefs, now) and _item_allowed(i, prefs, skip, now)
                and _uncached_explain(store, i)):
            return [k]
        return []
    for k, i in known.items():
        if (i.get("project") == scope.project and i.get("lane") == scope.lane
                and attention.in_front(i, prefs, now) and _item_allowed(i, prefs, skip, now)
                and _uncached_explain(store, i)):
            return [k]
    return []


def _resolve_upgrade_keys(
    store: Store, scope: UpgradeScope, event: Mapping[str, Any], now: float | None = None, *, is_verdict: bool = False,
) -> list[str]:
    known = _snap_items_by_key(store)
    prefs, skip = store.prefs(), autopilot.hands_off(store, list(known.values()))
    if scope.mode == "keys" and not is_verdict:
        return [k for k in scope.keys if k in known and _item_allowed(known[k], prefs, skip, now)]
    key = event.get("item_key")
    if not isinstance(key, str) or key not in known or not _item_allowed(known[key], prefs, skip, now):
        return []
    if scope.mode == "keys" and key not in scope.keys:
        return []
    wanted_ver = event.get("available")
    if isinstance(wanted_ver, str) and wanted_ver and known[key].get("available") != wanted_ver:
        return []
    return [key]


def _resolve_pin_keys(
    store: Store, scope: PinMcpScope, event: Mapping[str, Any], now: float | None = None, *, is_verdict: bool = False,
) -> list[str]:
    known = _snap_items_by_key(store)
    prefs, skip = store.prefs(), autopilot.hands_off(store, list(known.values()))
    allowed = [
        k for k in scope.keys
        if k in known and known[k].get("source") == "mcp" and known[k].get("fix_kind") == "pin"
        and _item_allowed(known[k], prefs, skip, now) and not store.active_job_for(k)
    ]
    if is_verdict:
        key = event.get("item_key")
        return [key] if isinstance(key, str) and key in allowed else []
    return allowed


def resolve_input(
    store: Store, defn: AutomationDef, *, event: Mapping[str, Any], now: float | None = None,
) -> dict[str, Any] | None:
    """Resolve the exact action input dict for `defn` and `event`, or None when there is nothing to do."""
    if has_unset_project(defn):
        return None
    scope = defn.scope
    is_verdict = defn.trigger.kind == "verdict"
    if isinstance(scope, NotifyScope):
        return {
            "digest": scope.digest,
            "snapshot_id": event.get("snapshot_id"),
            "prev_snapshot_id": event.get("prev_snapshot_id"),
        }
    if isinstance(scope, RefreshScope):
        return {"only": list(scope.only) if scope.only is not None else None, "auto_upgrade": False}
    if isinstance(scope, RecheckProjectScope):
        if scope.mode == "project":
            ev_proj = event.get("project")
            if is_verdict and isinstance(ev_proj, str) and ev_proj and ev_proj != scope.project:
                return None
            return {"project": scope.project}
        proj = event.get("project")
        return {"project": proj} if isinstance(proj, str) and proj else None
    if isinstance(scope, StartFixScope):
        keys = _resolve_fix_keys(store, scope, event, now, is_verdict=is_verdict)
        return {"keys": keys} if keys else None
    if isinstance(scope, ExplainItemScope):
        keys = _resolve_explain_keys(store, scope, event, now, is_verdict=is_verdict)
        return {"keys": keys} if keys else None
    if isinstance(scope, UpgradeScope):
        keys = _resolve_upgrade_keys(store, scope, event, now, is_verdict=is_verdict)
        return {"keys": keys} if keys else None
    if isinstance(scope, PinMcpScope):
        keys = _resolve_pin_keys(store, scope, event, now, is_verdict=is_verdict)
        return {"keys": keys} if keys else None
    if isinstance(scope, AppActionScope):
        return {"app": scope.app} if defn.action == "relaunch_app" else {"key": scope.app}
    return None


def _parse_firing_input(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row.get("input") or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _verified_firing_keys(
    store: Store, fids: tuple[int, ...], automation_id: int | None,
) -> set[str] | None:
    """Return allowed item keys across `fids`, cross-checked against recorded failure rows for verdict firings."""
    from . import verdict as averdict

    if not fids:
        return None
    allowed: set[str] = set()
    for fid in fids:
        row = astore.get_firing(store, fid)
        if row is None or (automation_id is not None and int(row["automation_id"]) != automation_id):
            return None
        parsed = _parse_firing_input(row)
        raw_keys = parsed.get("keys")
        in_keys = {k for k in raw_keys if isinstance(k, str) and k} if isinstance(raw_keys, list) else set()
        if row["event_kind"] == "verdict":
            found, rec_key, _ = averdict.recorded_failure_target(store, str(row["event_ref"] or ""))
            if found:
                if not rec_key or (in_keys and rec_key not in in_keys):
                    return None
                allowed.add(rec_key)
                continue
        if not in_keys:
            return None
        allowed |= in_keys
    return allowed


def _verified_firing_project(
    store: Store, fids: tuple[int, ...], automation_id: int | None,
) -> str | None:
    from . import verdict as averdict

    if len(fids) != 1:
        return None
    row = astore.get_firing(store, fids[0])
    if row is None or (automation_id is not None and int(row["automation_id"]) != automation_id):
        return None
    in_proj = _parse_firing_input(row).get("project")
    if row["event_kind"] == "verdict":
        found, _, rec_proj = averdict.recorded_failure_target(store, str(row["event_ref"] or ""))
        if found:
            if not rec_proj or (isinstance(in_proj, str) and in_proj and in_proj != rec_proj):
                return None
            return rec_proj
    return in_proj if isinstance(in_proj, str) and in_proj else None


def _keys_in_project_lane(
    known: dict[str, dict[str, Any]], keys: list[str], project: str, lane: str, *, fixable: bool,
) -> bool:
    for k in keys:
        item = known.get(k)
        if item is None or item.get("project") != project or item.get("lane") != lane:
            return False
        if fixable and not _is_fixable(item):
            return False
    return bool(keys)


def _verdict_keys_ok(
    store: Store, keys: list[str], fids: tuple[int, ...], automation_id: int | None,
) -> bool:
    allowed = _verified_firing_keys(store, fids, automation_id)
    return allowed is not None and set(keys) <= allowed


def _project_lane_ok(
    store: Store, scope: StartFixScope | ExplainItemScope, body: BaseModel,
    fids: tuple[int, ...], automation_id: int | None, *, is_verdict: bool, fixable: bool,
) -> bool:
    if not isinstance(body, FixIn) or (not fixable and len(body.keys) != 1):
        return False
    if not _keys_in_project_lane(_snap_items_by_key(store), body.keys, scope.project, scope.lane, fixable=fixable):
        return False
    return _verdict_keys_ok(store, body.keys, fids, automation_id) if (is_verdict and fids) else True


def input_in_scope(
    store: Store,
    defn: AutomationDef,
    body: BaseModel,
    *,
    firing_id: int | None = None,
    firing_ids: tuple[int, ...] = (),
    automation_id: int | None = None,
) -> bool:
    """Verify at `Actions.permit` time that `body` stays strictly inside `defn.scope`."""
    if has_unset_project(defn):
        return False
    fids = firing_ids if firing_ids else ((firing_id,) if firing_id is not None else ())
    scope, is_verdict = defn.scope, defn.trigger.kind == "verdict"
    if isinstance(scope, RefreshScope):
        return isinstance(body, RefreshIn) and body.auto_upgrade is False and (
            (tuple(body.only) if body.only is not None else None) == scope.only
        )
    if isinstance(scope, RecheckProjectScope):
        if not isinstance(body, ProjectIn):
            return False
        if scope.mode == "project":
            return body.project == scope.project
        return bool(body.project) and _verified_firing_project(store, fids, automation_id) == body.project
    if isinstance(scope, (StartFixScope, ExplainItemScope)):
        return _project_lane_ok(
            store, scope, body, fids, automation_id, is_verdict=is_verdict, fixable=isinstance(scope, StartFixScope),
        )
    if isinstance(scope, UpgradeScope):
        if not isinstance(body, KeysIn) or not body.keys:
            return False
        if scope.mode == "keys" and not (set(body.keys) <= set(scope.keys)):
            return False
        return _verdict_keys_ok(store, body.keys, fids, automation_id) if (scope.mode == "failed_item" or is_verdict) else True
    if isinstance(scope, PinMcpScope):
        if not isinstance(body, PinIn) or not body.keys or not (set(body.keys) <= set(scope.keys)):
            return False
        return _verdict_keys_ok(store, body.keys, fids, automation_id) if is_verdict else True
    if isinstance(scope, AppActionScope):
        if defn.action == "relaunch_app":
            return isinstance(body, RelaunchIn) and body.app == scope.app
        if defn.action == "open_app":
            return isinstance(body, KeyIn) and body.key == scope.app
    return False
