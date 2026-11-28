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


def matches_schedule(store: Store, conditions: ScheduleConditions, action: str) -> bool:
    """Evaluate `schedule` conditions against the latest snapshot for the target action."""
    if conditions.project is None and conditions.lane is None:
        return True
    items = list(_snap_items_by_key(store).values())
    prefs = store.prefs()
    candidates = [
        i for i in items
        if attention.in_front(i, prefs)
        and (conditions.project is None or i.get("project") == conditions.project)
        and (conditions.lane is None or i.get("lane") == conditions.lane)
    ]
    if action == "start_fix":
        return any(_is_fixable(i) and not store.active_fix_for(str(i["key"])) for i in candidates)
    if action == "explain_item":
        return any(_uncached_explain(store, i) for i in candidates)
    if action == "upgrade":
        skip = autopilot.hands_off(store, items)
        return any(autopilot.eligible(i, prefs, skip) for i in candidates)
    return bool(candidates)


def _resolve_fix_keys(store: Store, scope: StartFixScope) -> list[str]:
    prefs = store.prefs()
    return [
        k for k, i in _snap_items_by_key(store).items()
        if i.get("project") == scope.project and i.get("lane") == scope.lane
        and _is_fixable(i) and attention.in_front(i, prefs) and not store.active_fix_for(k)
    ][:100]


def _resolve_explain_keys(store: Store, scope: ExplainItemScope) -> list[str]:
    prefs = store.prefs()
    for k, i in _snap_items_by_key(store).items():
        if (i.get("project") == scope.project and i.get("lane") == scope.lane
                and attention.in_front(i, prefs) and _uncached_explain(store, i)):
            return [k]
    return []


def _resolve_upgrade_keys(store: Store, scope: UpgradeScope, event: Mapping[str, Any]) -> list[str]:
    known = _snap_items_by_key(store)
    if scope.mode == "keys":
        return [k for k in scope.keys if k in known]
    key = event.get("item_key")
    if not isinstance(key, str) or key not in known:
        return []
    wanted_ver = event.get("available")
    if isinstance(wanted_ver, str) and wanted_ver and known[key].get("available") != wanted_ver:
        return []
    return [key]


def resolve_input(store: Store, defn: AutomationDef, *, event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve the exact action input dict for `defn` and `event`, or None when there is nothing to do."""
    scope = defn.scope
    if isinstance(scope, NotifyScope):
        return {
            "digest": scope.digest,
            "snapshot_id": event.get("snapshot_id"),
            "prev_snapshot_id": event.get("prev_snapshot_id"),
        }
    if isinstance(scope, RefreshScope):
        return {"only": list(scope.only) if scope.only is not None else None, "auto_upgrade": False}
    if isinstance(scope, RecheckProjectScope):
        proj = scope.project if scope.mode == "project" else event.get("project")
        return {"project": proj} if isinstance(proj, str) and proj else None
    if isinstance(scope, StartFixScope):
        keys = _resolve_fix_keys(store, scope)
        return {"keys": keys} if keys else None
    if isinstance(scope, ExplainItemScope):
        keys = _resolve_explain_keys(store, scope)
        return {"keys": keys} if keys else None
    if isinstance(scope, UpgradeScope):
        keys = _resolve_upgrade_keys(store, scope, event)
        return {"keys": keys} if keys else None
    if isinstance(scope, PinMcpScope):
        known = _snap_items_by_key(store)
        keys = [
            k for k in scope.keys
            if k in known and known[k].get("source") == "mcp" and known[k].get("fix_kind") == "pin"
            and not store.active_job_for(k)
        ]
        return {"keys": keys} if keys else None
    if isinstance(scope, AppActionScope):
        return {"app": scope.app} if defn.action == "relaunch_app" else {"key": scope.app}
    return None


def _firing_input(store: Store, firing_id: int | None) -> dict[str, Any]:
    if firing_id is None:
        return {}
    row = astore.get_firing(store, firing_id)
    if row is None:
        return {}
    try:
        parsed = json.loads(row["input"] or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def input_in_scope(
    store: Store, defn: AutomationDef, body: BaseModel, *, firing_id: int | None = None,
) -> bool:
    """Verify at `Actions.permit` time that `body` stays strictly inside `defn.scope`."""
    scope = defn.scope
    if isinstance(scope, RefreshScope):
        if not isinstance(body, RefreshIn) or body.auto_upgrade is not False:
            return False
        got = tuple(body.only) if body.only is not None else None
        return got == scope.only
    if isinstance(scope, RecheckProjectScope):
        if not isinstance(body, ProjectIn):
            return False
        if scope.mode == "project":
            return body.project == scope.project
        return bool(body.project) and _firing_input(store, firing_id).get("project") == body.project
    if isinstance(scope, StartFixScope):
        return isinstance(body, FixIn) and _keys_in_project_lane(
            _snap_items_by_key(store), body.keys, scope.project, scope.lane, fixable=True,
        )
    if isinstance(scope, ExplainItemScope):
        return isinstance(body, FixIn) and len(body.keys) == 1 and _keys_in_project_lane(
            _snap_items_by_key(store), body.keys, scope.project, scope.lane, fixable=False,
        )
    if isinstance(scope, UpgradeScope):
        if not isinstance(body, KeysIn) or not body.keys:
            return False
        if scope.mode == "keys":
            return set(body.keys) <= set(scope.keys)
        allowed = _firing_input(store, firing_id).get("keys")
        return isinstance(allowed, list) and bool(allowed) and set(body.keys) <= set(allowed)
    if isinstance(scope, PinMcpScope):
        return isinstance(body, PinIn) and bool(body.keys) and set(body.keys) <= set(scope.keys)
    if isinstance(scope, AppActionScope):
        if defn.action == "relaunch_app":
            return isinstance(body, RelaunchIn) and body.app == scope.app
        if defn.action == "open_app":
            return isinstance(body, KeyIn) and body.key == scope.app
    return False
