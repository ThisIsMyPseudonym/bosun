"""API presentation and serialization helpers for Bosun automations."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from ..registry import SPECS, Invalid, NotFound, Risk, SaveAutomationIn, automation_id
from ..store import Store
from . import schedule, store as astore
from .schema import (UNSET_PROJECT, AppActionScope, AutomationDef, ExplainItemScope, NotifyScope, PinMcpScope,
                     RecheckProjectScope, RefreshConditions, RefreshScope, ScheduleConditions, ScheduleTrigger,
                     StartFixScope, UpgradeScope, describe_scope, grant_digest, grant_digest_matches, grant_expired,
                     grant_expires_at)

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


def _format_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}".lstrip(": ")
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return "; ".join(parts) or "invalid automation definition"


def _serialize_one(store: Store, aid: int, now: float) -> dict[str, Any]:
    row = astore.get(store, aid)
    if row is None:
        raise NotFound("No automation with that id")
    proj_map = {p["key"]: p["name"] for p in _snapshot_projects(store)}
    return serialize_automation(store, row, projects=proj_map, now=now)


def save_automation(
    store: Store, body: SaveAutomationIn, *, actor: str = "user", now: float | None = None,
) -> dict[str, Any]:
    when = time.time() if now is None else now
    preset: str | None = None
    if body.id is not None:
        existing = astore.get(store, body.id)
        if existing is None:
            raise NotFound("No automation with that id")
        preset = existing.get("preset")
    try:
        defn = AutomationDef.model_validate({
            "name": body.name,
            "trigger": body.trigger,
            "conditions": body.conditions,
            "action": body.action,
            "scope": body.scope,
            "mode": body.mode,
            "delay_s": body.wait_s,
            "max_per_day": body.max_per_day,
            "cooldown_s": body.cooldown_s,
            "model_jobs_per_day": body.model_jobs_per_day,
        })
    except ValidationError as exc:
        raise Invalid(_format_validation_error(exc)) from None
    if body.enabled and needs_project_pick(defn):
        raise Invalid("Pick a project before enabling this automation")
    need_confirm = needs_grant_confirmation(defn.action, defn.mode)
    if body.enabled and need_confirm and not body.confirm_grant:
        raise Invalid("Confirm the scope grant checkbox before enabling this automation")
    grant_ok = (body.confirm_grant or (body.enabled and not need_confirm)) and not needs_project_pick(defn)
    digest = grant_digest(defn) if grant_ok else ""
    aid = astore.save(
        store, defn, automation_id=body.id, preset=preset, enabled=body.enabled,
        grant_digest=digest, granted_by=actor if digest else "",
        granted_at=when if digest else None, now=when,
    )
    return _serialize_one(store, aid, when)


def set_automation_enabled(
    store: Store, automation_id: int, enabled: bool, *, confirm_grant: bool = False,
    actor: str = "user", now: float | None = None,
) -> dict[str, Any]:
    when = time.time() if now is None else now
    row = astore.get(store, automation_id)
    if row is None:
        raise NotFound("No automation with that id")
    if not enabled:
        astore.set_enabled(store, automation_id, False, now=when)
        return _serialize_one(store, automation_id, when)
    try:
        defn = astore.parse_def(row)
    except (ValueError, ValidationError) as exc:
        raise Invalid(f"Automation definition is invalid: {exc}") from None
    if needs_project_pick(defn):
        raise Invalid("Pick a project in the editor before enabling this automation")
    expected = grant_digest(defn)
    has_valid_grant = (
        grant_digest_matches(row.get("grant_digest"), expected)
        and not grant_expired(defn.action, defn.mode, row.get("granted_at"), now=when)
    )
    if needs_grant_confirmation(defn.action, defn.mode) and not has_valid_grant and not confirm_grant:
        raise Invalid("Confirm the scope grant before enabling this automation")
    if has_valid_grant:
        astore.set_enabled(store, automation_id, True, now=when)
    else:
        astore.save(
            store, defn, automation_id=automation_id, preset=row.get("preset"), enabled=True,
            grant_digest=expected, granted_by=actor, granted_at=when, now=when,
        )
    return _serialize_one(store, automation_id, when)


def renew_automation(
    store: Store, automation_id: int, *, actor: str = "user", now: float | None = None,
) -> dict[str, Any]:
    when = time.time() if now is None else now
    row = astore.get(store, automation_id)
    if row is None:
        raise NotFound("No automation with that id")
    try:
        defn = astore.parse_def(row)
    except (ValueError, ValidationError) as exc:
        raise Invalid(f"Automation definition is invalid: {exc}") from None
    if needs_project_pick(defn):
        raise Invalid("Pick a project in the editor before renewing this automation")
    if not grant_digest_matches(row.get("grant_digest"), grant_digest(defn)):
        raise Invalid("Save and confirm this automation before renewing its grant")
    astore.renew_grant(store, automation_id, granted_by=actor, now=when)
    return _serialize_one(store, automation_id, when)


def delete_automation(store: Store, automation_id: int, *, now: float | None = None) -> dict[str, Any]:
    if not astore.delete(store, automation_id, now=now):
        raise NotFound("No automation with that id")
    return {"deleted": automation_id}


def pause_automations(store: Store, paused: bool) -> dict[str, Any]:
    astore.set_paused(store, paused)
    return {"paused": astore.is_paused(store)}


def _automation_names(store: Store, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    args = tuple(sorted(ids))
    names: dict[int, str] = {}
    with store._tx() as db:
        for row in db.execute(f"SELECT id, name FROM automations WHERE id IN ({ph})", args).fetchall():
            names[int(row["id"])] = str(row["name"])
        missing = sorted(ids - set(names))
        if missing:
            mph = ",".join("?" * len(missing))
            for row in db.execute(
                f"SELECT automation_id, automation_name FROM automation_firings "
                f"WHERE automation_id IN ({mph}) ORDER BY id DESC",
                tuple(missing),
            ).fetchall():
                aid = int(row["automation_id"])
                if aid not in names and row["automation_name"]:
                    names[aid] = str(row["automation_name"])
    return names


def decorate_runs(store: Store, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach `actor_label` ('Automation: <name>') for any run started by `automation:<id>`."""
    ids = {aid for r in runs if (aid := automation_id(str(r.get("actor") or ""))) is not None}
    if not ids:
        return runs
    names = _automation_names(store, ids)
    out: list[dict[str, Any]] = []
    for run in runs:
        aid = automation_id(str(run.get("actor") or ""))
        if aid is not None:
            label = f"Automation: {names[aid]}" if aid in names else f"Automation #{aid}"
            out.append({**run, "actor_label": label})
        else:
            out.append(run)
    return out


def decorate_actor(store: Store, run: dict[str, Any]) -> dict[str, Any]:
    return decorate_runs(store, [run])[0]
