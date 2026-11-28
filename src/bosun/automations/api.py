"""API presentation and serialization helpers for Bosun automations."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from ..registry import SPECS, Risk
from ..store import Store
from . import schedule, store as astore
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
    ScheduleTrigger,
    StartFixScope,
    UpgradeScope,
    describe_scope,
    grant_expired,
    grant_expires_at,
)

_LANE_WORDS = {"now": "Do-now", "plan": "Plan", "watch": "Watch", "batch": "Batch"}
_DAY_WORDS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _snapshot_projects(store: Store) -> list[dict[str, Any]]:
    snap = store.latest_snapshot() or {}
    raw = (snap.get("meta") or {}).get("projects") or []
    return [
        {"key": str(p["key"]), "name": str(p.get("name") or p["key"]), "active": bool(p.get("active", True))}
        for p in raw
        if isinstance(p, dict) and p.get("key")
    ]


def _project_label(key: str | None, projects: dict[str, str]) -> str:
    if not key or key == UNSET_PROJECT:
        return "a chosen project"
    return projects.get(key) or key


def _trigger_phrase(defn: AutomationDef) -> str:
    trig = defn.trigger
    if trig.kind == "refresh":
        return "After a full refresh"
    if isinstance(trig, ScheduleTrigger):
        if trig.days:
            days = ", ".join(_DAY_WORDS.get(d, d) for d in trig.days)
            return f"On {days} at {trig.time}"
        return f"Daily at {trig.time}"
    if trig.kind == "verdict":
        return "When a failure verdict arrives"
    return "When an in-app update is waiting"


def _condition_phrase(defn: AutomationDef, projects: dict[str, str]) -> str:
    cond = defn.conditions
    if isinstance(cond, RefreshConditions):
        bits: list[str] = []
        if cond.new_in_lanes:
            lanes = ", ".join(_LANE_WORDS.get(x, x) for x in cond.new_in_lanes)
            bits.append(f"new items arrive in {lanes}")
        if cond.newly_exploited:
            bits.append("something is newly exploited")
        if cond.any_failed:
            bits.append("an update failed")
        return f"if {' and '.join(bits)}" if bits else ""
    if isinstance(cond, ScheduleConditions):
        if cond.project or cond.lane:
            proj = _project_label(cond.project, projects)
            lane = _LANE_WORDS.get(cond.lane or "now", cond.lane or "now")
            return f"if {proj} has {lane} items"
        return ""
    return ""


def _action_phrase(defn: AutomationDef, projects: dict[str, str]) -> str:
    scope = defn.scope
    verb = "offer to " if defn.mode == "ask" else ""
    if isinstance(scope, NotifyScope):
        return "notify with the What changed digest"
    if isinstance(scope, RefreshScope):
        which = ", ".join(scope.only) if scope.only else "everything"
        return f"{verb}re-scan {which}"
    if isinstance(scope, RecheckProjectScope):
        target = "the failed project" if scope.mode == "failed_project" else _project_label(scope.project, projects)
        return f"{verb}re-check {target}"
    if isinstance(scope, ExplainItemScope):
        return f"{verb}run 'Will this break?' on {_project_label(scope.project, projects)}"
    if isinstance(scope, UpgradeScope):
        what = "the failed item" if scope.mode == "failed_item" else f"{len(scope.keys)} listed item(s)"
        return f"{verb}upgrade {what}"
    if isinstance(scope, StartFixScope):
        return f"{verb}run Branch + test on {_project_label(scope.project, projects)}"
    if isinstance(scope, PinMcpScope):
        return f"{verb}pin {len(scope.keys)} MCP server(s)"
    assert isinstance(scope, AppActionScope)
    return f"offer to {defn.action.replace('_', ' ')} {scope.app}"


def describe_sentence(defn: AutomationDef, projects: dict[str, str]) -> str:
    """One plain sentence describing when this automation fires and what it does."""
    when = _trigger_phrase(defn)
    cond = _condition_phrase(defn, projects)
    act = _action_phrase(defn, projects)
    mid = f", {cond}, " if cond else ", "
    return f"{when}{mid}{act}."


def needs_project_pick(defn: AutomationDef) -> bool:
    scope = defn.scope
    if isinstance(scope, (StartFixScope, ExplainItemScope)) and scope.project == UNSET_PROJECT:
        return True
    if isinstance(scope, RecheckProjectScope) and scope.mode == "project" and scope.project == UNSET_PROJECT:
        return True
    return False


def needs_grant_confirmation(action: str, mode: str) -> bool:
    spec = SPECS.get(action)
    return mode == "run" and spec is not None and spec.risk > Risk.LOCAL


def serialize_automation(
    store: Store, row: dict[str, Any], *, projects: dict[str, str], now: float,
) -> dict[str, Any]:
    aid = int(row["id"])
    trigger = _parse_json_obj(row.get("trigger"))
    conditions = _parse_json_obj(row.get("conditions"))
    scope = _parse_json_obj(row.get("scope"))
    action = str(row["action"])
    mode = str(row["mode"])
    granted_at = float(row["granted_at"]) if row.get("granted_at") is not None else None
    try:
        defn: AutomationDef | None = astore.parse_def(row)
    except (ValueError, ValidationError):
        defn = None
    has_grant = bool(row.get("grant_digest"))
    expired = bool(has_grant and grant_expired(action, mode, granted_at, now=now))
    expires_at = grant_expires_at(action, mode, granted_at) if has_grant else None
    next_ts = (
        schedule.next_due(defn.trigger, now=now)
        if defn is not None and isinstance(defn.trigger, ScheduleTrigger)
        else None
    )
    return {
        "id": aid,
        "name": str(row["name"]),
        "preset": row.get("preset"),
        "enabled": bool(row["enabled"]),
        "paused_reason": str(row.get("paused_reason") or ""),
        "trigger": trigger,
        "conditions": conditions,
        "action": action,
        "scope": scope,
        "mode": mode,
        "wait_s": int(row.get("delay_s") or 0),
        "max_per_day": int(row.get("max_per_day") or 6),
        "cooldown_s": int(row.get("cooldown_s") or 900),
        "model_jobs_per_day": int(row.get("model_jobs_per_day") or 3),
        "grant_digest": str(row.get("grant_digest") or ""),
        "granted_by": str(row.get("granted_by") or ""),
        "granted_at": granted_at,
        "grant_expired": expired,
        "grant_expires_at": expires_at,
        "needs_grant": needs_grant_confirmation(action, mode),
        "needs_project": needs_project_pick(defn) if defn is not None else False,
        "sentence": describe_sentence(defn, projects) if defn is not None else str(row["name"]),
        "scope_sentence": describe_scope(defn) if defn is not None else "",
        "next_due": next_ts,
        "today_count": astore.fired_today(store, aid, now=now),
        "last_fired": astore.last_fired_at(store, aid),
        "created": row.get("created"),
        "updated": row.get("updated"),
    }


def serialize_firing(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "automation_id": int(row["automation_id"]),
        "automation_name": str(row.get("automation_name") or ""),
        "event_kind": str(row.get("event_kind") or ""),
        "event_ref": str(row.get("event_ref") or ""),
        "dedup_key": str(row.get("dedup_key") or ""),
        "depth": int(row.get("depth") or 0),
        "status": str(row.get("status") or ""),
        "due": row.get("due"),
        "deadline": row.get("deadline"),
        "input": _parse_json_obj(row.get("input")),
        "trace_id": str(row.get("trace_id") or ""),
        "reason": str(row.get("reason") or ""),
        "decided_by": str(row.get("decided_by") or ""),
        "decided": row.get("decided"),
        "created": row.get("created"),
        "finished": row.get("finished"),
    }


def list_automations(store: Store, *, demo: bool = False, now: float | None = None) -> dict[str, Any]:
    when = time.time() if now is None else now
    projects = _snapshot_projects(store)
    proj_map = {p["key"]: p["name"] for p in projects}
    items = [serialize_automation(store, r, projects=proj_map, now=when) for r in astore.list_all(store)]
    offers = [serialize_firing(f) for f in astore.list_firings(store, status="offered", limit=50)]
    return {
        "paused": astore.is_paused(store),
        "demo": demo,
        "automations": items,
        "projects": projects,
        "waiting_offers": offers,
    }


def list_firings(
    store: Store, *, automation_id: int | None = None, status: str | None = None,
    limit: int = 100, demo: bool = False,
) -> dict[str, Any]:
    rows = astore.list_firings(store, automation_id=automation_id, status=status, limit=limit)
    return {"demo": demo, "firings": [serialize_firing(r) for r in rows]}
