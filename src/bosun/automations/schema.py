"""Pydantic definitions, allow-list, canonical grant digest, 90-day expiry and plain-English scope text."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..registry import SPECS, Risk

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Lane = Literal["now", "plan", "watch", "batch"]
ActionLane = Literal["now", "batch"]
ActionMode = Literal["run", "ask"]
ActionName = Literal[
    "notify",
    "refresh",
    "recheck_project",
    "explain_item",
    "upgrade",
    "start_fix",
    "pin_mcp",
    "relaunch_app",
    "open_app",
]

ALLOWLIST: frozenset[str] = frozenset({
    "notify",
    "refresh",
    "recheck_project",
    "explain_item",
    "upgrade",
    "start_fix",
    "pin_mcp",
    "relaunch_app",
    "open_app",
})
SYSTEM_ACTIONS: frozenset[str] = frozenset({"relaunch_app", "open_app"})
MODEL_ACTIONS: frozenset[str] = frozenset({"start_fix", "explain_item"})
GRANT_TTL_S: int = 90 * 86400
UNSET_PROJECT: str = "__pick_project__"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_WEEKDAY_ORDER = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- triggers ------------------------------------------------------------------------------------------------
class RefreshTrigger(_Strict):
    kind: Literal["refresh"] = "refresh"


class ScheduleTrigger(_Strict):
    kind: Literal["schedule"] = "schedule"
    time: str = Field(description="Local time in 24-hour HH:MM format")
    days: tuple[Weekday, ...] = ()

    @field_validator("time")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        if not _TIME_RE.fullmatch(value):
            raise ValueError("time must be 24-hour HH:MM (00:00-23:59)")
        return value

    @field_validator("days")
    @classmethod
    def _dedup_days(cls, value: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        return tuple(sorted(dict.fromkeys(value), key=lambda d: _WEEKDAY_ORDER[d]))


class VerdictTrigger(_Strict):
    kind: Literal["verdict"] = "verdict"


class InAppUpdateTrigger(_Strict):
    kind: Literal["in_app_update"] = "in_app_update"


Trigger = RefreshTrigger | ScheduleTrigger | VerdictTrigger | InAppUpdateTrigger

TRIGGER_BY_KIND: dict[str, type[BaseModel]] = {
    "refresh": RefreshTrigger,
    "schedule": ScheduleTrigger,
    "verdict": VerdictTrigger,
    "in_app_update": InAppUpdateTrigger,
}


# --- conditions ----------------------------------------------------------------------------------------------
class RefreshConditions(_Strict):
    new_in_lanes: tuple[Lane, ...] = ()
    newly_exploited: bool = False
    any_failed: bool = False


class ScheduleConditions(_Strict):
    project: str | None = Field(default=None, max_length=1000)
    lane: ActionLane | None = None


class VerdictConditions(_Strict):
    categories: tuple[str, ...] = ()
    transient: bool | None = None
    needs_person: bool | None = None
    min_confidence: Literal["low", "medium", "high"] | None = None
    next_step_kind: Literal["recipe", "retry", "rescan", "runbook", "agent_prompt", "none"] | None = None
    run_kind: Literal["upgrade", "fix", "refresh"] | None = None
    source: str | None = Field(default=None, max_length=40)
    project: str | None = Field(default=None, max_length=1000)
    verdict_source: Literal["rules", "cache", "model"] | None = None


class InAppUpdateConditions(_Strict):
    apps: tuple[str, ...] = ()


Conditions = RefreshConditions | ScheduleConditions | VerdictConditions | InAppUpdateConditions

CONDITIONS_BY_KIND: dict[str, type[BaseModel]] = {
    "refresh": RefreshConditions,
    "schedule": ScheduleConditions,
    "verdict": VerdictConditions,
    "in_app_update": InAppUpdateConditions,
}


# --- scopes --------------------------------------------------------------------------------------------------
class NotifyScope(_Strict):
    digest: Literal["what_changed"] = "what_changed"


class RefreshScope(_Strict):
    only: tuple[str, ...] | None = None


class RecheckProjectScope(_Strict):
    mode: Literal["project", "failed_project"] = "project"
    project: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _require_project(self) -> RecheckProjectScope:
        if self.mode == "project" and not self.project.strip():
            raise ValueError("project is required when mode is 'project'")
        return self


class ExplainItemScope(_Strict):
    project: str = Field(min_length=1, max_length=1000)
    lane: ActionLane = "now"


class UpgradeScope(_Strict):
    mode: Literal["failed_item", "keys"] = "failed_item"
    keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _require_keys(self) -> UpgradeScope:
        if self.mode == "keys" and not self.keys:
            raise ValueError("keys must be non-empty when mode is 'keys'")
        return self


class StartFixScope(_Strict):
    project: str = Field(min_length=1, max_length=1000)
    lane: ActionLane = "now"


class PinMcpScope(_Strict):
    keys: tuple[str, ...] = Field(min_length=1, max_length=200)


class AppActionScope(_Strict):
    app: str = Field(min_length=1, max_length=1000)


Scope = (
    NotifyScope
    | RefreshScope
    | RecheckProjectScope
    | ExplainItemScope
    | UpgradeScope
    | StartFixScope
    | PinMcpScope
    | AppActionScope
)

SCOPE_BY_ACTION: dict[str, type[BaseModel]] = {
    "notify": NotifyScope,
    "refresh": RefreshScope,
    "recheck_project": RecheckProjectScope,
    "explain_item": ExplainItemScope,
    "upgrade": UpgradeScope,
    "start_fix": StartFixScope,
    "pin_mcp": PinMcpScope,
    "relaunch_app": AppActionScope,
    "open_app": AppActionScope,
}


# --- full definition -----------------------------------------------------------------------------------------
def _coerce_field(raw: Any, expected: type[BaseModel]) -> Any:
    if isinstance(raw, dict):
        return expected.model_validate(raw)
    return raw


class AutomationDef(_Strict):
    name: str = Field(min_length=1, max_length=120)
    trigger: Trigger
    conditions: Conditions
    action: ActionName
    scope: Scope
    mode: ActionMode = "run"
    delay_s: int = Field(default=0, ge=0, le=86400)
    max_per_day: int = Field(default=6, ge=1, le=48)
    cooldown_s: int = Field(default=900, ge=0, le=86400)
    model_jobs_per_day: int = Field(default=3, ge=0, le=48)

    @model_validator(mode="before")
    @classmethod
    def _coerce_nested(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        trig = out.get("trigger")
        kind = trig.get("kind") if isinstance(trig, dict) else getattr(trig, "kind", None)
        if isinstance(kind, str) and kind in TRIGGER_BY_KIND:
            out["trigger"] = _coerce_field(trig, TRIGGER_BY_KIND[kind])
            if "conditions" in out:
                out["conditions"] = _coerce_field(out["conditions"], CONDITIONS_BY_KIND[kind])
        action = out.get("action")
        if isinstance(action, str) and action in SCOPE_BY_ACTION and "scope" in out:
            out["scope"] = _coerce_field(out["scope"], SCOPE_BY_ACTION[action])
        return out

    @model_validator(mode="after")
    def _validate_pairing_and_policy(self) -> AutomationDef:
        if self.action not in ALLOWLIST:
            raise ValueError(f"action {self.action!r} is not allowed for automations")
        expected_cond = CONDITIONS_BY_KIND[self.trigger.kind]
        if not isinstance(self.conditions, expected_cond):
            raise ValueError(f"conditions must match trigger {self.trigger.kind!r}")
        expected_scope = SCOPE_BY_ACTION[self.action]
        if not isinstance(self.scope, expected_scope):
            raise ValueError(f"scope must match action {self.action!r}")
        if self.action in SYSTEM_ACTIONS and self.mode != "ask":
            raise ValueError(f"system action {self.action!r} must always ask first (mode='ask')")
        if self.action == "notify" and self.mode != "run":
            raise ValueError("notify automations always use mode='run'")
        return self


def is_install_action(action: str) -> bool:
    spec = SPECS.get(action)
    return spec is not None and spec.risk == Risk.INSTALL


def has_unset_project(defn: AutomationDef) -> bool:
    """True when any scope or condition still carries the `UNSET_PROJECT` placeholder sentinel."""
    scope = defn.scope
    if isinstance(scope, (StartFixScope, ExplainItemScope)) and scope.project == UNSET_PROJECT:
        return True
    if isinstance(scope, RecheckProjectScope) and scope.mode == "project" and scope.project == UNSET_PROJECT:
        return True
    cond = defn.conditions
    return isinstance(cond, (ScheduleConditions, VerdictConditions)) and cond.project == UNSET_PROJECT


def grant_digest(defn: AutomationDef) -> str:
    """Canonical SHA-256 over the owner's rule definition (trigger, conditions, action, scope, mode, limits)."""
    payload = {
        "trigger": defn.trigger.model_dump(mode="json"),
        "conditions": defn.conditions.model_dump(mode="json"),
        "action": defn.action,
        "scope": defn.scope.model_dump(mode="json"),
        "mode": defn.mode,
        "delay_s": defn.delay_s,
        "max_per_day": defn.max_per_day,
        "cooldown_s": defn.cooldown_s,
        "model_jobs_per_day": defn.model_jobs_per_day,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def grant_digest_matches(stored: Any, expected: str) -> bool:
    """Constant-time UTF-8 byte comparison of a stored grant digest; non-ASCII or malformed values return False."""
    if not isinstance(stored, str) or not stored:
        return False
    return secrets.compare_digest(stored.encode("utf-8"), expected.encode("utf-8"))


def grant_expires_at(action: str, mode: str, granted_at: float | None) -> float | None:
    """90-day expiry timestamp for runs-on-its-own INSTALL grants; None for LOCAL, notify, or asks-first."""
    if mode != "run" or not is_install_action(action):
        return None
    if granted_at is None:
        return None
    return float(granted_at) + GRANT_TTL_S


def grant_expired(action: str, mode: str, granted_at: float | None, now: float | None = None) -> bool:
    """True when a runs-on-its-own INSTALL grant has no timestamp or has reached its 90-day expiry."""
    if mode != "run" or not is_install_action(action):
        return False
    if granted_at is None:
        return True
    when = time.time() if now is None else now
    return when >= float(granted_at) + GRANT_TTL_S


def describe_scope(defn: AutomationDef) -> str:
    """One plain sentence describing the granted scope for the owner's grant checkbox."""
    cap = f"at most {defn.max_per_day} time{'s' if defn.max_per_day != 1 else ''} a day"
    scope = defn.scope
    if isinstance(scope, NotifyScope):
        return f"Send a 'What changed' summary line after a refresh, {cap}."
    if isinstance(scope, RefreshScope):
        which = ", ".join(scope.only) if scope.only else "all collectors"
        return f"Re-scan {which} without running Autopilot, {cap}."
    if isinstance(scope, RecheckProjectScope):
        target = "the failed run's project" if scope.mode == "failed_project" else scope.project
        return f"Re-check project {target}, {cap}."
    if isinstance(scope, ExplainItemScope):
        proj = "a chosen project" if scope.project == UNSET_PROJECT else scope.project
        return f"Run 'Will this break?' on {scope.lane} items in {proj}, {cap} (up to {defn.model_jobs_per_day} AI jobs/day)."
    if isinstance(scope, UpgradeScope):
        what = "the failed item, same version" if scope.mode == "failed_item" else f"{len(scope.keys)} listed item(s)"
        return f"Upgrade {what}, {cap}; Bosun's waiting period and checks still apply."
    if isinstance(scope, StartFixScope):
        proj = "a chosen project" if scope.project == UNSET_PROJECT else scope.project
        return f"Run Branch + test on {scope.lane} items in {proj}, {cap} (sandboxed, never pushes; up to {defn.model_jobs_per_day} AI jobs/day)."
    if isinstance(scope, PinMcpScope):
        return f"Pin {len(scope.keys)} listed MCP server(s), {cap}; the 3-day waiting period still applies."
    return f"Offer to {defn.action.replace('_', ' ')} {scope.app} (asks for your approval each time)."
